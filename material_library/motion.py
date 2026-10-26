"""Local motion analysis used to locate action intervals inside a shot."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class MotionSignal:
    times: np.ndarray
    energy: np.ndarray
    brightness: np.ndarray
    sharpness: np.ndarray


@dataclass(frozen=True)
class Candidate:
    start: float
    end: float
    action_score: float
    quality_score: float
    motion_start: float
    motion_end: float


def compute_signal(times: list[float], frames: list[np.ndarray]) -> MotionSignal:
    if not frames:
        empty = np.zeros(0, dtype=np.float32)
        return MotionSignal(empty, empty, empty, empty)
    brightness = np.array([float(frame.mean()) for frame in frames], dtype=np.float32)
    sharpness = np.array(
        [float(frame.var()) for frame in frames], dtype=np.float32
    )
    energy = np.zeros(len(frames), dtype=np.float32)
    for index in range(1, len(frames)):
        diff = np.abs(
            frames[index].astype(np.int16) - frames[index - 1].astype(np.int16)
        )
        energy[index] = float(diff.mean())
    # The first sample has no predecessor; mirror the second sample so a
    # motion that starts immediately is not clipped.
    if len(frames) > 1:
        energy[0] = energy[1]
    return MotionSignal(
        times=np.asarray(times, dtype=np.float32),
        energy=energy,
        brightness=brightness,
        sharpness=sharpness,
    )


def _smooth(values: np.ndarray, window: int) -> np.ndarray:
    if len(values) < 3 or window < 2:
        return values
    window = min(window, len(values))
    kernel = np.ones(window, dtype=np.float32) / window
    return np.convolve(values, kernel, mode="same")


def _regions(mask: np.ndarray) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(mask):
        if value and start is None:
            start = index
        elif not value and start is not None:
            regions.append((start, index))
            start = None
    if start is not None:
        regions.append((start, len(mask)))
    return regions


def find_candidates(
    signal: MotionSignal,
    *,
    shot_start: float,
    shot_end: float,
    target_duration: float = 3.0,
    min_duration: float = 1.0,
    max_duration: float = 8.0,
    sample_fps: int = 8,
    max_candidates: int = 3,
    exact_action: bool = False,
) -> list[Candidate]:
    """Return up to ``max_candidates`` non-overlapping best windows.

    The energy signal is normalized per shot, so a static talking head and a
    busy product demo are both segmented by their own relative motion.

    With ``exact_action=True``, return the detected action interval itself
    (with only a minimum-duration safety pad). This is used for ordinary
    source footage: a clip should contain the complete gesture without
    padding it with a different action just to reach three seconds.
    """
    shot_duration = max(0.0, shot_end - shot_start)
    if len(signal.times) < 2 or shot_duration <= 0:
        return []

    energy = _smooth(signal.energy, max(2, round(sample_fps * 0.35)))
    mean = float(energy.mean())
    std = float(energy.std())
    peak = float(energy.max())
    threshold = max(mean + 0.45 * std, peak * 0.22, 1e-6)
    mask = energy >= threshold

    gap_samples = max(1, round(sample_fps * 0.30))
    regions = _regions(mask)
    merged: list[tuple[int, int]] = []
    for start, end in regions:
        if merged and start - merged[-1][1] <= gap_samples:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))

    durations = [
        float(signal.times[min(end, len(signal.times) - 1)] - signal.times[start])
        for start, end in merged
    ]
    # Drop single-sample blips that are almost certainly compression noise or
    # a one-frame flash; they only inflate the candidate list.
    min_region = max(1.5 / max(sample_fps, 1), min(0.2, shot_duration * 0.1))
    regions = [
        region for region, duration in zip(merged, durations) if duration >= min_region
    ]
    if not regions:
        regions = [(0, len(signal.times))]

    # Requested clip length. Never exceed the shot; never fall below
    # ``min_duration`` unless the shot itself is shorter than that.
    target = min(max_duration, max(target_duration, min_duration))
    target = min(target, shot_duration)
    floor = min(min_duration, shot_duration)
    exact_floor = min(0.2, shot_duration)

    # Quality: sharpness relative to the shot, penalize extremely dark frames.
    sharp = signal.sharpness
    median_sharpness = float(np.median(sharp))
    # A flat frame (wall, white background) has ~0 variance. Treat it as a
    # neutral quality instead of dividing by a near-zero reference.
    has_texture = median_sharpness > 1.0
    sharp_ref = median_sharpness if has_texture else 1.0
    candidates: list[Candidate] = []
    for region_start, region_end in regions:
        if region_start >= len(signal.times):
            continue
        region_end = min(region_end, len(signal.times))
        region_start_time = float(signal.times[region_start])
        region_end_time = float(signal.times[region_end - 1])
        region_span = max(0.0, region_end_time - region_start_time)

        # Finished edits can use the requested window, while ordinary footage
        # should retain the complete motion interval and avoid unrelated pads.
        if exact_action:
            search_start = region_start_time
            search_end = min(shot_end, region_end_time + 1.0 / max(sample_fps, 1))
            if search_end - search_start < exact_floor:
                pad = (exact_floor - (search_end - search_start)) / 2.0
                search_start = max(shot_start, search_start - pad)
                search_end = min(shot_end, search_end + pad)
                search_start = max(shot_start, search_end - exact_floor)
        elif region_span < target:
            pad = (target - region_span) / 2.0
            search_start = max(shot_start, region_start_time - pad)
            search_end = min(shot_end, search_start + target)
            search_start = max(shot_start, search_end - target)
        else:
            search_start, search_end = region_start_time, region_end_time
        if search_end - search_start < (exact_floor if exact_action else floor) * 0.99:
            continue

        first = int(np.searchsorted(signal.times, search_start, side="left"))
        last = int(np.searchsorted(signal.times, search_end, side="right"))
        first = max(0, min(first, len(signal.times) - 2))
        last = max(first + 2, min(last, len(signal.times)))

        # Rolling sum keeps the scan O(n) even for long shots.
        local = energy[first:last].astype(np.float64)
        requested_window = (
            search_end - search_start if exact_action else target
        )
        window_samples = max(2, min(len(local), round(requested_window * sample_fps)))
        window_sum = np.convolve(local, np.ones(window_samples), mode="valid")
        if window_sum.size == 0:
            continue

        peak_energy = float(np.max(local)) if local.size else 0.0
        best_score = -1.0
        best_mean = 0.0
        best_low = 0
        for offset, total in enumerate(window_sum):
            overlap_lo = max(first + offset, region_start)
            overlap_hi = min(first + offset + window_samples, region_end)
            overlap = max(0, overlap_hi - overlap_lo)
            # Mean motion inside the window drives the score; overlapping the
            # detected region breaks ties in favour of keeping the action.
            mean_energy = float(total) / window_samples
            score = mean_energy
            score += 0.15 * (overlap / window_samples) * max(peak_energy, 1.0)
            if score > best_score or (score == best_score and mean_energy > best_mean):
                best_score = score
                best_mean = mean_energy
                best_low = offset

        low_index = first + best_low
        high_index = min(last, low_index + window_samples)
        start_time = float(signal.times[low_index])
        end_time = float(signal.times[high_index - 1])
        end_time = min(end_time + (1.0 / max(sample_fps, 1)), shot_end)
        if not exact_action:
            end_time = min(end_time, start_time + max_duration)
        start_time = max(start_time, shot_start)
        if end_time - start_time < (exact_floor if exact_action else floor) * 0.9:
            continue

        lo, hi = low_index, high_index
        if has_texture:
            quality = float(np.mean(np.clip(sharp[lo:hi] / sharp_ref, 0.0, 2.0))) / 2.0
        else:
            quality = 0.5
        brightness_mean = float(np.mean(signal.brightness[lo:hi]))
        if brightness_mean < 25 or brightness_mean > 235:
            quality *= 0.6
        if peak_energy > 0:
            action = 0.35 + 0.65 * min(1.0, best_mean / peak_energy)
        else:
            action = 0.2
        candidates.append(
            Candidate(
                start=start_time,
                end=end_time,
                action_score=round(action, 4),
                quality_score=round(max(0.0, min(1.0, quality)), 4),
                motion_start=region_start_time,
                motion_end=min(region_end_time, shot_end),
            )
        )

    candidates.sort(key=lambda item: (item.action_score, item.quality_score), reverse=True)
    selected: list[Candidate] = []
    for candidate in candidates:
        if all(
            candidate.end <= other.start + 0.05 or candidate.start >= other.end - 0.05
            for other in selected
        ):
            selected.append(candidate)
        if len(selected) >= max_candidates:
            break
    selected.sort(key=lambda item: item.start)
    return selected
