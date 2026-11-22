"""Annotate the opponent name and clan of Clash Royale battle videos.

The script samples the video at a low rate, decides whether a battle is in
progress, and draws a green box around the opponent player name and a red box
around the opponent clan name. It writes an annotated MP4 plus a per-video JSON
and a summary JSON.

All input and output locations are parameters; by default outputs are written
below the current working directory (``annotated/`` and ``results/``) or below
an explicit ``--out-dir``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import site
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import cv2
import numpy as np

LogFn = Callable[[str], None]

ANNOTATED_SUBDIR = "annotated"
RESULTS_SUBDIR = "results"
DEFAULT_VIDEO_DIR = Path("source_videos")
BV_ID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")


def _default_log(message: str) -> None:
    print(message, flush=True)


def _register_cuda_dlls() -> None:
    """Make pip-installed cuBLAS/cuDNN DLLs visible to onnxruntime.

    Everything here is best effort: machines without CUDA (or without the
    ``nvidia-*`` wheels) must keep working on the CPU provider.
    """
    try:
        package_dirs = list(site.getsitepackages())
    except Exception:
        package_dirs = []
    for base in package_dirs:
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
            folder = os.path.join(base, sub)
            if not os.path.isdir(folder):
                continue
            try:
                os.add_dll_directory(folder)
            except (AttributeError, OSError):
                pass
    try:
        import onnxruntime

        preload = getattr(onnxruntime, "preload_dlls", None)
        if callable(preload):
            preload()
    except Exception:
        pass


_register_cuda_dlls()

from rapidocr_onnxruntime import RapidOCR


# Optional metadata for well-known public videos; unknown inputs simply omit it.
VIDEO_SOURCES = {
    "BV1h5qrYNEyz": {
        "creator": "小雾Fusion",
        "title": "史上最强进攻！100+费用！这一波谁能防？",
    },
    "BV12193YJE2V": {
        "creator": "小雾Fusion",
        "title": "我的卡组双核了！烧坏的就是对手的CPU！",
    },
    "BV1H7z3YzEQt": {
        "creator": "小雾Fusion",
        "title": "在加速最多的模式玩移速最快的八张卡！这就是皇室战争的闪电战！",
    },
    "BV1tf4y1M78o": {
        "creator": "小雾Fusion",
        "title": "⚡️这 是 什 么⚡️",
    },
    "BV1BR34zSE38": {
        "creator": "小雾Fusion",
        "title": "全游戏目前最恐怖的组合！",
    },
    "BV1mRpX6RERw": {
        "creator": "塔米糕ovo",
        "title": "什么叫进化雷胖可以配墓碑女皇？等压雷胖+超级数值墓碑女皇 恐怖如斯！【皇室战争】",
    },
}

UI_WORDS = (
    "剩余时间",
    "搜索对手",
    "取消",
    "下一张",
    "已用卡组数",
    "已用",
    "卡组数",
    "卡牌数",
    "直播",
)
COUNTDOWN_RE = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}")


@dataclass
class TextBox:
    box: list[int]
    text: str
    confidence: float

    @property
    def x1(self) -> int:
        return self.box[0]

    @property
    def y1(self) -> int:
        return self.box[1]

    @property
    def x2(self) -> int:
        return self.box[2]

    @property
    def y2(self) -> int:
        return self.box[3]

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def center_x(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def center_y(self) -> float:
        return (self.y1 + self.y2) / 2


class BattleRecognizer:
    def __init__(self, log: LogFn = _default_log) -> None:
        try:
            self.ocr = RapidOCR(
                det_use_cuda=True,
                cls_use_cuda=True,
                rec_use_cuda=True,
                det_limit_side_len=960,
                det_limit_type="max",
            )
            self.device = "cuda"
        except Exception as error:
            log(f"[recognizer] CUDA unavailable ({error}); using CPU")
            self.ocr = RapidOCR()
            self.device = "cpu"

    @staticmethod
    def card_strip_gate(frame: np.ndarray) -> tuple[bool, float]:
        height, width = frame.shape[:2]
        roi = frame[int(height * 0.78) :]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        blue = cv2.inRange(hsv, (88, 55, 35), (135, 255, 255))
        row_coverage = np.mean(blue > 0, axis=1)
        blue_ratio = float(np.mean(blue > 0))
        panel_rows = float(np.mean(row_coverage > 0.16))
        bottom = frame[int(height * 0.95) :]
        bottom_hsv = cv2.cvtColor(bottom, cv2.COLOR_BGR2HSV)
        bottom_blue = cv2.inRange(bottom_hsv, (88, 55, 35), (135, 255, 255))
        bottom_ratio = float(np.mean(bottom_blue > 0))
        ok = blue_ratio >= 0.055 and panel_rows >= 0.08 and bottom_ratio >= 0.18
        return ok, blue_ratio

    @staticmethod
    def timer_panel_evidence(frame: np.ndarray, label: TextBox) -> bool:
        height, width = frame.shape[:2]
        x1 = max(0, label.x1 - int(width * 0.025))
        x2 = min(width, max(label.x2 + int(width * 0.035), int(width * 0.96)))
        y1 = max(0, label.y1 - int(height * 0.004))
        y2 = min(int(height * 0.27), label.y2 + int(height * 0.065))
        if x2 <= x1 or y2 <= y1:
            return False

        roi = frame[y1:y2, x1:x2]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        dark = (hsv[:, :, 2] < 115) & (hsv[:, :, 1] < 185)
        dark_ratio = float(np.mean(dark))

        digit_y1 = min(roi.shape[0], label.y2 - y1)
        lower = roi[digit_y1:]
        if lower.size == 0:
            return False
        lower_hsv = cv2.cvtColor(lower, cv2.COLOR_BGR2HSV)
        bright = (lower_hsv[:, :, 2] > 155) & (lower_hsv[:, :, 1] < 130)
        bright_ratio = float(np.mean(bright))
        return dark_ratio >= 0.08 and bright_ratio >= 0.003

    UI_LABELS = {
        "对战日志", "对战", "锦标赛", "胜利", "失败", "平局", "练习", "分享",
        "观看", "卡组", "商店", "活动", "设置", "返回", "确定", "取消", "领取",
        "搜索对手", "开始游戏", "继续", "再来一局", "关闭",
    }

    @staticmethod
    def _normalized(text: str) -> str:
        return re.sub(r"\s+", "", text).lower()

    def _ocr_top(self, frame: np.ndarray) -> list[TextBox]:
        height, width = frame.shape[:2]
        top = frame[: int(height * 0.26)]
        scale = min(2.0, max(1.0, 1000.0 / max(1, width)))
        if scale > 1.01:
            enlarged = cv2.resize(top, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        else:
            enlarged = top
        result, _ = self.ocr(enlarged)
        boxes: list[TextBox] = []
        for polygon, text, confidence in result or []:
            xs = [float(point[0]) / scale for point in polygon]
            ys = [float(point[1]) / scale for point in polygon]
            boxes.append(
                TextBox(
                    [
                        max(0, round(min(xs))),
                        max(0, round(min(ys))),
                        round(max(xs)),
                        round(max(ys)),
                    ],
                    text.strip(),
                    float(confidence),
                )
            )
        return boxes

    def _find_timer(self, frame: np.ndarray, boxes: list[TextBox]) -> tuple[bool, str, TextBox | None]:
        _, width = frame.shape[:2]
        labels = []
        countdowns = []
        for box in boxes:
            if box.x1 < width * 0.54:
                continue
            normalized = self._normalized(box.text)
            direct_label = any(word in normalized for word in ("剩余时间", "剩餘時間", "剩余时", "剩餘時", "余时间", "餘時間", "加时赛", "加時賽"))
            fuzzy_label = box.center_y < frame.shape[0] * 0.04 and len(normalized) <= 6 and any(char in normalized for char in "时间時間加剩余餘")
            if direct_label or fuzzy_label:
                labels.append(box)
            if COUNTDOWN_RE.search(normalized) or (re.search(r"[:：]\s*\d{2}", normalized) and box.center_y < frame.shape[0] * 0.12):
                countdowns.append(box)

        for label in labels:
            if self.timer_panel_evidence(frame, label):
                return True, label.text, label

        for countdown in countdowns:
            synthetic_label = TextBox(
                [countdown.x1, max(0, countdown.y1 - 8), countdown.x2, countdown.y1],
                "countdown",
                countdown.confidence,
            )
            if self.timer_panel_evidence(frame, synthetic_label):
                return True, countdown.text, countdown
        return False, "", None

    def _candidate_boxes(self, frame: np.ndarray, boxes: list[TextBox]) -> list[TextBox]:
        height, width = frame.shape[:2]
        watermark_rows = [box for box in boxes if "bilibili" in self._normalized(box.text)]
        candidates = []
        for box in boxes:
            normalized = self._normalized(box.text)
            if box.confidence < 0.30 or not normalized:
                continue
            if len(normalized) < 2 and (box.center_x > width * 0.38 or box.center_y > height * 0.12):
                continue
            if not any(character.isalpha() for character in normalized):
                continue
            if any(word in normalized for word in UI_WORDS) or COUNTDOWN_RE.search(normalized):
                continue
            if "bilibili" in normalized:
                continue
            if box.height < height * 0.004 or box.height > height * 0.052:
                continue
            if box.width > width * 0.68:
                continue
            if box.center_x > width * 0.72 and box.center_y < height * 0.14:
                continue
            if re.search(r"^\d+[级級].{0,2}盟", normalized):
                continue
            is_watermark = False
            for watermark in watermark_rows:
                same_row = abs(box.center_y - watermark.center_y) <= max(box.height, watermark.height)
                nearby = abs(box.center_x - watermark.center_x) < width * 0.42
                if same_row and nearby:
                    is_watermark = True
                    break
            if not is_watermark:
                candidates.append(box)
        return candidates

    def _find_opponent_pair(
        self, frame: np.ndarray, boxes: list[TextBox]
    ) -> tuple[TextBox, TextBox] | None:
        height, width = frame.shape[:2]
        candidates = self._candidate_boxes(frame, boxes)
        best_pair = None
        best_score = -math.inf

        for name in candidates:
            for clan in candidates:
                if name is clan or clan.y1 < name.y2 - height * 0.012:
                    continue
                gap = clan.y1 - name.y2
                if gap > height * 0.025 or gap < -height * 0.012:
                    continue
                if min(name.height, clan.height) / max(name.height, clan.height) < 0.32:
                    continue

                overlap = max(0, min(name.x2, clan.x2) - max(name.x1, clan.x1))
                overlap_ratio = overlap / max(1, min(name.width, clan.width))
                center_delta = abs(name.center_x - clan.center_x)
                if overlap_ratio < 0.12 and center_delta > max(name.width, clan.width) * 0.58:
                    continue
                if center_delta > max(name.width, clan.width) * 0.72 + width * 0.025:
                    continue

                pair_center = (name.center_x + clan.center_x) / 2
                if width * 0.38 < pair_center < width * 0.57:
                    continue
                alignment = 1 - min(1.0, center_delta / max(1, width * 0.25))
                proximity = math.exp(-max(0, gap) / max(1, height * 0.016))
                size_ratio = min(name.height, clan.height) / max(name.height, clan.height)
                confidence = (name.confidence + clan.confidence) / 2
                score = (
                    2.8 * overlap_ratio
                    + 1.8 * alignment
                    + 1.4 * proximity
                    + 0.5 * size_ratio
                    + 0.4 * confidence
                )
                if score > best_score:
                    best_score = score
                    best_pair = (name, clan)
        if best_pair:
            merged = self._merge_name_tokens(width, candidates, best_pair[0], best_pair[1])
            if merged is not None:
                return merged, best_pair[1]
            return best_pair
        inferred = self._infer_missing_name(frame, candidates)
        if inferred:
            return inferred
        return self._infer_missing_clan(frame, candidates)

    def _infer_missing_clan(
        self, frame: np.ndarray, candidates: list[TextBox]
    ) -> tuple[TextBox, TextBox] | None:
        height, width = frame.shape[:2]
        for name in candidates:
            if name.center_x >= width * 0.38 or name.y1 > height * 0.025:
                continue
            if name.y2 > height * 0.09:
                continue
            line_height = max(name.height * 0.72, height * 0.014)
            clan_x1 = name.x1
            clan_y1 = min(height - 1, round(name.y2 + height * 0.004))
            clan_y2 = min(height, round(clan_y1 + line_height))
            clan_width = max(int(name.width * 0.55), int(line_height * 3))
            clan_width = min(clan_width, width - clan_x1)
            clan = TextBox(
                [clan_x1, clan_y1, clan_x1 + clan_width, clan_y2], "", 0.0
            )
            return name, clan
        return None

    SHIELD_TEMPLATE = np.array(
        [
            [0.50, 0.00], [0.86, 0.06], [0.94, 0.20], [0.94, 0.52],
            [0.88, 0.70], [0.72, 0.86], [0.50, 1.00], [0.28, 0.86],
            [0.12, 0.70], [0.06, 0.52], [0.06, 0.20], [0.14, 0.06],
        ],
        dtype=np.float32,
    ) * 100.0

    @classmethod
    def _badge_slot_present(cls, frame: np.ndarray, name: TextBox, clan: TextBox | None) -> bool:
        for scale, pad in ((2.4, 0.30), (3.2, 0.15)):
            if cls._shield_in_slot(frame, name, clan, scale, pad):
                return True
        return False

    @classmethod
    def _shield_in_slot(
        cls, frame: np.ndarray, name: TextBox, clan: TextBox | None, scale: float, pad: float
    ) -> bool:
        badge_width = max(6, int(name.height * scale))
        x2 = max(1, int(name.x1) - 1)
        x1 = max(0, x2 - badge_width)
        y1 = max(0, int(name.y1 - name.height * pad))
        bottom = clan.y2 if clan else name.y2
        y2 = min(frame.shape[0], int(bottom + name.height * pad))
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return False
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        factor = 1.0
        if gray.shape[1] < 60:
            factor = 140.0 / gray.shape[1]
            gray = cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 60, 150)
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return False
        roi_height, roi_width = gray.shape[:2]
        spread = float(gray.std())
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:6]:
            bx, by, bw, bh = cv2.boundingRect(contour)
            width_ratio = bw / max(1, roi_width)
            height_ratio = bh / max(1, roi_height)
            solidity = cv2.contourArea(contour) / max(1, bw * bh)
            right_edge = x1 + (bx + bw) / factor
            top_edge = y1 + by / factor
            bottom_edge = y1 + (by + bh) / factor
            if abs(right_edge - name.x1) > name.height * 0.45:
                continue
            if abs(top_edge - name.y1) > name.height * 0.55:
                continue
            if abs(bottom_edge - bottom) > name.height * 0.85:
                continue
            if solidity >= 0.50:
                if width_ratio < 0.45 or height_ratio < 0.60:
                    continue
                if cv2.matchShapes(contour, cls.SHIELD_TEMPLATE, cv2.CONTOURS_MATCH_I1, 0.0) <= 0.6:
                    return True
            elif width_ratio >= 0.75 and height_ratio >= 0.75 and spread >= 30.0:
                return True
        return False

    @staticmethod
    def _merge_name_tokens(
        width: int, candidates: list[TextBox], name: TextBox, clan: TextBox
    ) -> TextBox | None:
        current = name
        changed = False
        for _ in range(3):
            extended = None
            best_gap = None
            for box in candidates:
                if box is current or box is clan:
                    continue
                overlap = min(current.y2, box.y2) - max(current.y1, box.y1)
                if overlap < min(current.height, box.height) * 0.55:
                    continue
                gap = box.x1 - current.x2
                if gap < -current.height * 0.6 or gap > current.height * 0.8:
                    continue
                if box.x2 <= current.x2:
                    continue
                if box.x2 > width * 0.70:
                    continue
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    extended = box
            if extended is None:
                break
            current = TextBox(
                [
                    current.x1,
                    min(current.y1, extended.y1),
                    extended.x2,
                    max(current.y2, extended.y2),
                ],
                current.text + extended.text,
                min(current.confidence, extended.confidence),
            )
            changed = True
        return current if changed else None

    def _infer_missing_name(
        self, frame: np.ndarray, candidates: list[TextBox]
    ) -> tuple[TextBox, TextBox] | None:
        height, width = frame.shape[:2]
        best_pair = None
        best_score = 0.0
        for clan in candidates:
            if clan.center_y > height * 0.23:
                continue
            on_left = clan.center_x < width * 0.35
            on_right = clan.center_x > width * 0.65
            if not (on_left or on_right):
                continue

            crop_width = max(int(width * 0.27), clan.width * 3)
            if on_left:
                crop_x1 = max(0, clan.x1)
                crop_x2 = min(width, crop_x1 + crop_width)
            else:
                crop_x2 = min(width, clan.x2 + int(width * 0.10))
                crop_x1 = max(0, crop_x2 - crop_width)
            crop_y2 = max(0, clan.y1 - int(height * 0.002))
            crop_y1 = max(0, clan.y1 - int(height * 0.060))
            if crop_x2 - crop_x1 < 20 or crop_y2 - crop_y1 < 12:
                continue

            crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]
            enlarged = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            result, _ = self.ocr(enlarged, use_det=False, use_cls=False, use_rec=True)
            if not result:
                continue
            text, confidence = result[0]
            normalized = self._normalized(text)
            if (
                confidence < 0.25
                or len(normalized) < 2
                or not any(character.isalpha() for character in normalized)
                or any(word in normalized for word in UI_WORDS)
                or "bilibili" in normalized
                or COUNTDOWN_RE.search(normalized)
            ):
                continue

            line_height = max(clan.height * 1.25, height * 0.018)
            estimated_width = max(clan.width, int(len(normalized) * line_height * 0.60))
            estimated_width = min(estimated_width, crop_x2 - crop_x1)
            if on_left:
                name_x1, name_x2 = crop_x1, crop_x1 + estimated_width
            else:
                name_x1, name_x2 = crop_x2 - estimated_width, crop_x2
            name_y2 = max(0, clan.y1 - int(height * 0.004))
            name_y1 = max(0, round(name_y2 - line_height))
            name = TextBox(
                [name_x1, name_y1, name_x2, name_y2], text, float(confidence)
            )
            if confidence > best_score:
                best_score = confidence
                best_pair = (name, clan)
        return best_pair

    def detect(self, frame: np.ndarray) -> dict:
        card_ok, card_blue_ratio = self.card_strip_gate(frame)
        result = {
            "battle": False,
            "card_strip": card_ok,
            "card_blue_ratio": round(card_blue_ratio, 4),
            "timer": False,
            "timer_text": "",
            "name": None,
            "clan": None,
            "_timer_label": None,
        }
        if not card_ok:
            return result

        boxes = self._ocr_top(frame)
        timer_ok, timer_text, timer_label = self._find_timer(frame, boxes)
        pair = None
        if not timer_ok:
            height, width = frame.shape[:2]
            visual_label = TextBox(
                [int(width * 0.87), int(height * 0.004), int(width * 0.97), int(height * 0.024)],
                "timer-panel", 0.0
            )
            candidate_pair = self._find_opponent_pair(frame, boxes)
            if candidate_pair:
                candidate_name, candidate_clan = candidate_pair
                pair_center = (candidate_name.center_x + candidate_clan.center_x) / 2
                if pair_center < width * 0.38 and self.timer_panel_evidence(frame, visual_label):
                    timer_ok = True
                    timer_text = "visual-panel"
                    timer_label = visual_label
                    pair = candidate_pair
        result["timer"] = timer_ok
        result["timer_text"] = timer_text
        result["_timer_label"] = timer_label
        result["battle"] = False
        if not card_ok:
            return result

        if pair is None:
            pair = self._find_opponent_pair(frame, boxes)
        if pair and (pair[0].text in self.UI_LABELS or pair[1].text in self.UI_LABELS):
            pair = None
            result["battle"] = False
            return result
        if pair and not self._badge_slot_present(frame, pair[0], pair[1]):
            result["battle"] = False
            return result
        if pair:
            name, clan = pair
            result["name"] = {
                "text": name.text,
                "box": name.box,
                "confidence": round(name.confidence, 4),
            }
            result["clan"] = {
                "text": clan.text,
                "box": clan.box,
                "confidence": round(clan.confidence, 4),
            }
            result["battle"] = True
        return result


def padded_box(box: list[int], width: int, height: int) -> tuple[int, int, int, int]:
    padding = max(3, round(width / 300))
    return (
        max(0, box[0] - padding),
        max(0, box[1] - padding),
        min(width - 1, box[2] + padding),
        min(height - 1, box[3] + padding),
    )


def draw_boxes(frame: np.ndarray, pair: dict) -> np.ndarray:
    height, width = frame.shape[:2]
    thickness = max(2, round(width / 360))
    for key, color in (("name", (60, 230, 60)), ("clan", (60, 60, 240))):
        item = pair.get(key)
        if not item:
            continue
        x1, y1, x2, y2 = padded_box(item["box"], width, height)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness, cv2.LINE_AA)
    return frame


def _output_dirs(out_dir: Path) -> tuple[Path, Path]:
    annotated_dir = out_dir / ANNOTATED_SUBDIR
    results_dir = out_dir / RESULTS_SUBDIR
    annotated_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    return annotated_dir, results_dir


def resolve_video_source(value: str, video_dir: Path) -> Path:
    """Accept either an existing video file or a BV id resolved under video_dir."""
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    by_id = video_dir / f"{value}.mp4"
    if by_id.is_file():
        return by_id.resolve()
    raise FileNotFoundError(
        f"Cannot find video {value!r} (looked for a file and for {by_id})"
    )


def process_video(
    source_path: Path,
    out_dir: Path,
    recognizer: BattleRecognizer,
    sample_fps: float = 1.0,
    entry_samples: int = 2,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    part_index: int | None = None,
    log: LogFn = _default_log,
) -> dict:
    video_id = source_path.stem
    annotated_dir, results_dir = _output_dirs(out_dir)
    suffix = f"_part{part_index}" if part_index is not None else ""
    annotated_path = annotated_dir / f"{video_id}_annotated{suffix}.mp4"
    result_path = results_dir / f"{video_id}{suffix}.json"
    battle_image_path = results_dir / f"{video_id}_battle{suffix}.jpg"
    nonbattle_image_path = results_dir / f"{video_id}_nonbattle{suffix}.jpg"

    capture = cv2.VideoCapture(str(source_path))
    if not capture.isOpened():
        raise RuntimeError(f"Cannot open video: {source_path}")
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frame_count / fps if frame_count else 0.0
    sample_step = max(1, round(fps / sample_fps))
    start_frame = int(round(start_seconds * fps / sample_step)) * sample_step
    end_frame = frame_count
    if end_seconds is not None:
        end_frame = min(frame_count, int(round(end_seconds * fps)))
    if start_frame > 0:
        capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    output_scale = min(1.0, 540 / width)
    output_width = max(2, round(width * output_scale / 2) * 2)
    output_height = max(2, round(height * output_scale / 2) * 2)
    output_fps = min(15.0, fps)
    ffmpeg_command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-nostats",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{width}x{height}",
        "-r",
        f"{fps:.6f}",
        "-i",
        "pipe:0",
        "-an",
        "-vf",
        f"scale={output_width}:{output_height},fps={output_fps:.6f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        "160k",
        "-maxrate",
        "240k",
        "-bufsize",
        "480k",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(annotated_path),
    ]
    writer = subprocess.Popen(
        ffmpeg_command, stdin=subprocess.PIPE, stderr=subprocess.PIPE
    )

    active = False
    positive_streak = 0
    last_pair = None
    last_pair_frame = -10**9
    last_timer_label = None
    samples = []
    opponents = Counter()
    boxed_frames = 0
    battle_saved = False
    nonbattle_saved = False
    frame_index = start_frame

    log(f"[{video_id}] {width}x{height}, {fps:.3f} fps, {duration:.1f}s")
    while frame_index < end_frame:
        ok, frame = capture.read()
        if not ok:
            break

        sampled = frame_index % sample_step == 0
        if sampled:
            detection = recognizer.detect(frame)
            timer_label = detection.pop("_timer_label")
            if timer_label is not None:
                last_timer_label = timer_label
            if detection["battle"]:
                positive_streak += 1
                if positive_streak >= entry_samples:
                    active = True
            else:
                positive_streak = 0
                active = False
                last_pair = None
                last_timer_label = None

            if detection["name"] and detection["clan"]:
                last_pair = {"name": detection["name"], "clan": detection["clan"]}
                last_pair_frame = frame_index
                opponents[(detection["name"]["text"], detection["clan"]["text"])] += 1

            detection.update(
                {
                    "time": round(frame_index / fps, 3),
                    "active": active,
                }
            )
            samples.append(detection)
            if not detection["battle"] and not nonbattle_saved:
                cv2.imwrite(str(nonbattle_image_path), frame)
                nonbattle_saved = True

        visible_pair = None
        if (
            active
            and last_pair is not None
            and frame_index - last_pair_frame <= max(sample_step * 2, round(fps * 1.5))
        ):
            card_ok, _ = recognizer.card_strip_gate(frame)
            timer_ok = (
                last_timer_label is not None
                and recognizer.timer_panel_evidence(frame, last_timer_label)
            )
            if card_ok and timer_ok:
                visible_pair = last_pair

        if visible_pair is not None:
            draw_boxes(frame, visible_pair)
            boxed_frames += 1
            if not battle_saved:
                cv2.imwrite(str(battle_image_path), frame)
                battle_saved = True

        writer.stdin.write(frame.tobytes())
        frame_index += 1
        if frame_index % max(1, round(fps * 30)) == 0:
            log(
                f"[{video_id}] {frame_index / fps:.0f}/{duration:.0f}s, "
                f"samples={len(samples)}, boxed={boxed_frames / fps:.1f}s"
            )

    capture.release()
    writer.stdin.close()
    ffmpeg_errors = writer.stderr.read().decode("utf-8", errors="replace").strip()
    if writer.wait() != 0:
        raise RuntimeError(f"FFmpeg failed for {video_id}: {ffmpeg_errors}")

    result = {
        "video_id": video_id,
        "source_url": (
            f"https://www.bilibili.com/video/{video_id}" if BV_ID_RE.match(video_id) else ""
        ),
        **VIDEO_SOURCES.get(video_id, {}),
        "width": width,
        "height": height,
        "fps": round(fps, 6),
        "output_width": output_width,
        "output_height": output_height,
        "output_fps": output_fps,
        "output_video_bitrate": "160k",
        "duration_seconds": round(frame_index / fps, 3),
        "sample_fps": sample_fps,
        "entry_samples": entry_samples,
        "sample_count": len(samples),
        "raw_battle_samples": sum(sample["battle"] for sample in samples),
        "boxed_seconds": round(boxed_frames / fps, 3),
        "opponents": [
            {"name": name, "clan": clan, "sample_count": count}
            for (name, clan), count in opponents.most_common()
        ],
        "annotated_video": annotated_path.name,
        "samples": samples,
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[{video_id}] completed: {result_path}")
    return {key: value for key, value in result.items() if key != "samples"}


def _part_paths(
    annotated_dir: Path, results_dir: Path, video_id: str, part_index: int
) -> tuple[Path, Path, Path, Path]:
    suffix = f"_part{part_index}"
    return (
        annotated_dir / f"{video_id}_annotated{suffix}.mp4",
        results_dir / f"{video_id}{suffix}.json",
        results_dir / f"{video_id}_battle{suffix}.jpg",
        results_dir / f"{video_id}_nonbattle{suffix}.jpg",
    )


def process_video_parallel(
    source_path: Path,
    out_dir: Path,
    workers: int,
    sample_fps: float = 1.0,
    entry_samples: int = 2,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    log: LogFn = _default_log,
) -> dict:
    video_id = source_path.stem
    annotated_dir, results_dir = _output_dirs(out_dir)

    capture = cv2.VideoCapture(str(source_path))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    capture.release()
    duration = frame_count / fps if frame_count else 0.0
    clip_start = max(0.0, start_seconds)
    clip_end = duration if end_seconds is None else min(duration, end_seconds)
    clip_length = max(0.0, clip_end - clip_start)
    workers = max(1, min(workers, max(1, int(clip_length // 30))))

    boundaries = [
        round(clip_start + clip_length * index / workers, 2)
        for index in range(workers + 1)
    ]
    processes = []
    for part_index in range(workers):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--video",
            str(source_path),
            "--out-dir",
            str(out_dir),
            "--part",
            str(part_index),
            "--start",
            str(boundaries[part_index]),
            "--end",
            str(boundaries[part_index + 1]),
            "--sample-fps",
            str(sample_fps),
            "--entry-samples",
            str(entry_samples),
            "--no-summary",
        ]
        processes.append(
            (part_index, subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
        )
        log(
            f"[{video_id}] part {part_index}: "
            f"{boundaries[part_index]}s - {boundaries[part_index + 1]}s"
        )

    failed = []
    for part_index, process in processes:
        output = process.communicate()[0].decode("utf-8", errors="replace")
        if process.returncode != 0:
            failed.append((part_index, output.strip().splitlines()[-8:]))
    if failed:
        raise RuntimeError(f"Parallel parts failed for {video_id}: {failed}")

    concat_list = annotated_dir / f"_{video_id}_parts.txt"
    concat_list.write_text(
        "\n".join(
            f"file '{_part_paths(annotated_dir, results_dir, video_id, index)[0].as_posix()}'"
            for index in range(workers)
        ),
        encoding="utf-8",
    )
    annotated_path = annotated_dir / f"{video_id}_annotated.mp4"
    concat_command = [
        "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", "-movflags", "+faststart", str(annotated_path),
    ]
    if subprocess.run(concat_command).returncode != 0:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                "-i", str(concat_list), "-an", "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", "160k", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(annotated_path),
            ],
            check=True,
        )
    concat_list.unlink()

    samples = []
    boxed_seconds = 0.0
    summary = None
    battle_image = None
    nonbattle_image = None
    for part_index in range(workers):
        _, json_path, part_battle, part_nonbattle = _part_paths(
            annotated_dir, results_dir, video_id, part_index
        )
        data = json.loads(json_path.read_text(encoding="utf-8"))
        samples.extend(data.pop("samples"))
        boxed_seconds += data.get("boxed_seconds", 0.0)
        if summary is None:
            summary = data
        if battle_image is None and part_battle.exists():
            battle_image = part_battle
        if nonbattle_image is None and part_nonbattle.exists():
            nonbattle_image = part_nonbattle
    samples.sort(key=lambda item: item["time"])

    opponents = Counter()
    for sample in samples:
        if sample["name"] and sample["clan"]:
            opponents[(sample["name"]["text"], sample["clan"]["text"])] += 1

    if battle_image is not None:
        battle_image.replace(results_dir / f"{video_id}_battle.jpg")
    if nonbattle_image is not None:
        nonbattle_image.replace(results_dir / f"{video_id}_nonbattle.jpg")

    result = {
        **(summary or {"video_id": video_id}),
        "source_url": (
            f"https://www.bilibili.com/video/{video_id}" if BV_ID_RE.match(video_id) else ""
        ),
        "duration_seconds": round(samples[-1]["time"], 3) if samples else 0.0,
        "sample_count": len(samples),
        "raw_battle_samples": sum(sample["battle"] for sample in samples),
        "boxed_seconds": round(boxed_seconds, 3),
        "opponents": [
            {"name": name, "clan": clan, "sample_count": count}
            for (name, clan), count in opponents.most_common()
        ],
        "annotated_video": annotated_path.name,
        "parallel_workers": workers,
        "samples": samples,
    }
    (results_dir / f"{video_id}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for part_index in range(workers):
        for part_path in _part_paths(annotated_dir, results_dir, video_id, part_index):
            if part_path.exists():
                part_path.unlink()
    log(f"[{video_id}] merged: {results_dir / (video_id + '.json')}")
    return {key: value for key, value in result.items() if key != "samples"}


def annotate_videos(
    videos: Sequence[str],
    out_dir: Path | str | None = None,
    video_dir: Path | str | None = None,
    sample_fps: float = 1.0,
    entry_samples: int = 2,
    parallel: int = 0,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    write_summary: bool = True,
    log: LogFn = _default_log,
) -> list[dict]:
    """Annotate one or more videos (paths or BV ids) and write JSON results."""
    out_root = (Path(out_dir).expanduser() if out_dir else Path.cwd()).resolve()
    video_root = Path(video_dir).expanduser() if video_dir else DEFAULT_VIDEO_DIR
    if not video_root.is_absolute():
        video_root = (Path.cwd() / video_root).resolve()
    _output_dirs(out_root)

    sources = [resolve_video_source(value, video_root) for value in videos]
    if parallel and parallel > 1:
        summaries = [
            process_video_parallel(
                source,
                out_root,
                parallel,
                sample_fps,
                entry_samples,
                start_seconds,
                end_seconds,
                log,
            )
            for source in sources
        ]
    else:
        recognizer = BattleRecognizer(log=log)
        summaries = [
            process_video(
                source,
                out_root,
                recognizer,
                sample_fps,
                entry_samples,
                start_seconds,
                end_seconds,
                None,
                log,
            )
            for source in sources
        ]

    if write_summary:
        summary_path = out_root / RESULTS_SUBDIR / "summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "video_count": len(summaries),
                    "sample_fps": sample_fps,
                    "entry_samples": entry_samples,
                    "videos": summaries,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log(f"Summary: {summary_path}")
    return summaries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Annotate the opponent name and clan in Clash Royale battle videos."
    )
    parser.add_argument(
        "--video",
        nargs="+",
        required=True,
        metavar="PATH_OR_BV",
        help="One or more MP4 paths, or Bilibili BV ids resolved under --video-dir.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Root folder for annotated/ and results/ (default: current directory).",
    )
    parser.add_argument(
        "--video-dir",
        type=Path,
        default=DEFAULT_VIDEO_DIR,
        help="Folder used to resolve BV ids (default: ./source_videos).",
    )
    parser.add_argument("--sample-fps", type=float, default=1.0)
    parser.add_argument("--entry-samples", type=int, default=2)
    parser.add_argument(
        "--parallel",
        type=int,
        default=0,
        help="Split each video into N parts and annotate them concurrently.",
    )
    parser.add_argument("--start", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--end", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--part", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--no-summary", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    out_root = (args.out_dir.expanduser() if args.out_dir else Path.cwd()).resolve()
    video_root = args.video_dir.expanduser()
    if not video_root.is_absolute():
        video_root = (Path.cwd() / video_root).resolve()

    if args.part is not None:
        source = resolve_video_source(args.video[0], video_root)
        _output_dirs(out_root)
        process_video(
            source,
            out_root,
            BattleRecognizer(),
            args.sample_fps,
            args.entry_samples,
            args.start,
            args.end,
            args.part,
        )
        return

    annotate_videos(
        args.video,
        out_dir=out_root,
        video_dir=video_root,
        sample_fps=args.sample_fps,
        entry_samples=args.entry_samples,
        parallel=args.parallel,
        start_seconds=args.start,
        end_seconds=args.end,
        write_summary=not args.no_summary,
    )


if __name__ == "__main__":
    main()
