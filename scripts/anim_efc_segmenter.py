#!/usr/bin/env python3
"""Content-agnostic adaptive EFC (animpack-v3) segmentation and prefetch simulator.

Models the ESP32-S3 firmware's D-slot zero-copy prefetch queue (D=2) directly:
  - Small/medium clips (<= single_segment_max_bytes) remain a single segment.
  - Larger clips are segmented dynamically using exact binary search per segment
    against a live prefetch-queue byte budget:
      * seg 0 is capped by first_segment_max_bytes for snappy cold-start.
      * seg k (k >= 1) is capped by min(segment_max_bytes, prefetch_budget(k))
        where prefetch_budget(k) equals the bytes that can finish loading before
        seg k's scheduled playback start tau[k], accounting for queue depth D
        and safety_margin_s.
  - For looping clips, if the last segment is too short to cover wrap-around
    prefetch of seg 0, the final two segments are rebalanced automatically.

This module intentionally takes an `encode_span(start_frame, frame_count) -> bytes`
callback so it stays decoupled from any specific EFC Python binding and can be
reused directly by WatcheRobot_sd or host test suites.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass

ANIM_PACK_MAGIC = b"ANPK"
ANIM_PACK_VERSION_EFC = 3
ANIM_FRAME_FLAG_EFC = 0x0002
ANIM_FRAME_FLAG_EFC_SWAPPED = 0x0004
DEFAULT_PREFETCH_DEPTH = 2

PACK_HEADER_FMT = "<4sHHHHBBHIII"
PACK_HEADER_SIZE = struct.calcsize(PACK_HEADER_FMT)
FRAME_DESC_FMT = "<IIHH"
FRAME_DESC_SIZE = struct.calcsize(FRAME_DESC_FMT)


@dataclass(frozen=True)
class SegmentBudgetConfig:
    """Hardware-derived physical budget parameters (no per-expression rules)."""

    single_segment_max_bytes: int = 512 * 1024
    first_segment_max_bytes: int = 145 * 1024
    segment_max_bytes: int = 450 * 1024
    min_segment_budget_bytes: int = 32 * 1024
    prefetch_rate_bps: float = 240.0 * 1024.0
    prefetch_depth: int = DEFAULT_PREFETCH_DEPTH
    safety_margin_s: float = 0.15
    min_segment_frames: int = 4
    max_segment_frames: int = 96
    tail_merge_slack: float = 1.08

    def __post_init__(self) -> None:
        if self.single_segment_max_bytes <= 0:
            raise ValueError("single_segment_max_bytes must be positive")
        if self.first_segment_max_bytes <= 0:
            raise ValueError("first_segment_max_bytes must be positive")
        if self.segment_max_bytes <= 0:
            raise ValueError("segment_max_bytes must be positive")
        if self.min_segment_budget_bytes <= 0:
            raise ValueError("min_segment_budget_bytes must be positive")
        if self.prefetch_rate_bps <= 0:
            raise ValueError("prefetch_rate_bps must be positive")
        if self.prefetch_depth <= 0:
            raise ValueError("prefetch_depth must be positive")
        if self.safety_margin_s < 0:
            raise ValueError("safety_margin_s must be non-negative")
        if self.min_segment_frames <= 0:
            raise ValueError("min_segment_frames must be positive")
        if self.max_segment_frames < self.min_segment_frames:
            raise ValueError("max_segment_frames must be >= min_segment_frames")
        if self.tail_merge_slack < 1.0:
            raise ValueError("tail_merge_slack must be >= 1.0")


@dataclass(frozen=True)
class EncodedSegment:
    start_frame: int
    frame_count: int
    duration_ms: int
    payload: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


@dataclass(frozen=True)
class PrefetchStepReport:
    step: int
    segment_index: int
    size_bytes: int
    duration_ms: int
    start_s: float
    ready_s: float
    due_s: float
    margin_ms: float
    hit: bool


@dataclass(frozen=True)
class PrefetchSimulationReport:
    hits: int
    total_transitions: int
    min_margin_ms: float
    stall_free: bool
    steps: tuple[PrefetchStepReport, ...]


@dataclass(frozen=True)
class SegmentationResult:
    segments: tuple[EncodedSegment, ...]
    single_pass_report: PrefetchSimulationReport
    loop_report: PrefetchSimulationReport
    encode_calls: int

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def total_payload_bytes(self) -> int:
        return sum(seg.size_bytes for seg in self.segments)

    @property
    def first_segment_bytes(self) -> int:
        return self.segments[0].size_bytes if self.segments else 0

    @property
    def max_segment_bytes(self) -> int:
        return max((seg.size_bytes for seg in self.segments), default=0)


def simulate_prefetch_queue(
    segment_sizes: Sequence[int],
    segment_durations_ms: Sequence[int],
    *,
    prefetch_rate_bps: float = 240.0 * 1024.0,
    prefetch_depth: int = DEFAULT_PREFETCH_DEPTH,
    loop: bool = False,
) -> PrefetchSimulationReport:
    """Simulate the firmware's D-slot sequential EFC prefetch queue.

    Segment 0 is loaded synchronously at t=0. Every subsequent segment k >= 1
    starts prefetching at max(t_ready[k-1], tau[max(0, k - prefetch_depth)])
    and must finish before its playback start time tau[k].
    """
    if len(segment_sizes) != len(segment_durations_ms):
        raise ValueError("segment_sizes and segment_durations_ms must have the same length")
    if prefetch_rate_bps <= 0 or prefetch_depth <= 0:
        raise ValueError("prefetch_rate_bps and prefetch_depth must be positive")

    seg_count = len(segment_sizes)
    if seg_count <= 1:
        return PrefetchSimulationReport(
            hits=0,
            total_transitions=0,
            min_margin_ms=float("inf"),
            stall_free=True,
            steps=(),
        )

    total_steps = seg_count * 2 if loop else seg_count
    tau = [0.0] * (total_steps + 1)
    for k in range(total_steps):
        tau[k + 1] = tau[k] + (segment_durations_ms[k % seg_count] / 1000.0)

    t_ready = [0.0] * total_steps
    steps: list[PrefetchStepReport] = []
    hits = 0
    min_margin_ms = float("inf")

    for k in range(1, total_steps):
        seg_idx = k % seg_count
        size_b = segment_sizes[seg_idx]
        dur_ms = segment_durations_ms[seg_idx]
        t_start = max(t_ready[k - 1], tau[max(0, k - prefetch_depth)])
        t_ready[k] = t_start + (size_b / prefetch_rate_bps)
        margin_ms = (tau[k] - t_ready[k]) * 1000.0
        min_margin_ms = min(min_margin_ms, margin_ms)
        hit = margin_ms >= 0.0
        if hit:
            hits += 1
        steps.append(
            PrefetchStepReport(
                step=k,
                segment_index=seg_idx,
                size_bytes=size_b,
                duration_ms=dur_ms,
                start_s=t_start,
                ready_s=t_ready[k],
                due_s=tau[k],
                margin_ms=margin_ms,
                hit=hit,
            )
        )

    total_transitions = total_steps - 1
    return PrefetchSimulationReport(
        hits=hits,
        total_transitions=total_transitions,
        min_margin_ms=min_margin_ms,
        stall_free=(hits == total_transitions),
        steps=tuple(steps),
    )


def _simulate_segments(
    segments: Sequence[EncodedSegment],
    config: SegmentBudgetConfig,
    *,
    loop: bool,
) -> PrefetchSimulationReport:
    return simulate_prefetch_queue(
        [seg.size_bytes for seg in segments],
        [seg.duration_ms for seg in segments],
        prefetch_rate_bps=config.prefetch_rate_bps,
        prefetch_depth=config.prefetch_depth,
        loop=loop,
    )


def plan_adaptive_efc_segments(
    frame_delays_ms: Sequence[int],
    encode_span: Callable[[int, int], bytes],
    *,
    loop: bool = False,
    config: SegmentBudgetConfig = SegmentBudgetConfig(),
) -> SegmentationResult:
    """Partition an animation into streaming EFC segments using live queue budgets."""
    total_frames = len(frame_delays_ms)
    if total_frames == 0:
        raise ValueError("frame_delays_ms must not be empty")
    if any(delay <= 0 for delay in frame_delays_ms):
        raise ValueError("All frame delays must be positive")

    cache: dict[tuple[int, int], bytes] = {}
    encode_calls = 0

    def enc_cached(start: int, count: int) -> bytes:
        nonlocal encode_calls
        key = (start, count)
        blob = cache.get(key)
        if blob is None:
            blob = encode_span(start, count)
            if not blob:
                raise ValueError(f"encode_span({start}, {count}) returned empty payload")
            cache[key] = blob
            encode_calls += 1
        return blob

    def make_segment(start: int, count: int, payload: bytes) -> EncodedSegment:
        return EncodedSegment(
            start_frame=start,
            frame_count=count,
            duration_ms=int(sum(frame_delays_ms[start : start + count])),
            payload=payload,
        )

    def max_fit_frames(start: int, budget_bytes: int, lo_f: int, hi_f: int) -> tuple[int, bytes]:
        b_lo = enc_cached(start, lo_f)
        if len(b_lo) > budget_bytes:
            # High-entropy / complex GIF fallback: search below min_segment_frames
            # down to a single frame (<= ~85 KB raw for 206x206 RGB565), guaranteeing
            # every future GIF stays within ESP32-S3 segment and PSRAM budgets.
            if lo_f > 1:
                b_one = enc_cached(start, 1)
                if len(b_one) > budget_bytes:
                    return 1, b_one
                sub_lo, sub_hi = 1, lo_f
                best_sub_n, best_sub_blob = 1, b_one
                while sub_lo + 1 < sub_hi:
                    mid = (sub_lo + sub_hi) // 2
                    blob_mid = enc_cached(start, mid)
                    if len(blob_mid) <= budget_bytes:
                        sub_lo = mid
                        best_sub_n, best_sub_blob = mid, blob_mid
                    else:
                        sub_hi = mid
                return best_sub_n, best_sub_blob
            return lo_f, b_lo
        if lo_f >= hi_f:
            return lo_f, b_lo
        b_hi = enc_cached(start, hi_f)
        if len(b_hi) <= budget_bytes:
            return hi_f, b_hi

        lo, hi = lo_f, hi_f
        best_n, best_blob = lo_f, b_lo
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            blob_mid = enc_cached(start, mid)
            if len(blob_mid) <= budget_bytes:
                lo = mid
                best_n, best_blob = mid, blob_mid
            else:
                hi = mid
        return best_n, best_blob

    whole_blob = enc_cached(0, total_frames)
    if len(whole_blob) <= config.single_segment_max_bytes or total_frames <= 1:
        segments = (make_segment(0, total_frames, whole_blob),)
        single_rep = _simulate_segments(segments, config, loop=False)
        loop_rep = _simulate_segments(segments, config, loop=True)
        return SegmentationResult(
            segments=segments,
            single_pass_report=single_rep,
            loop_report=loop_rep,
            encode_calls=encode_calls,
        )

    segments_list: list[EncodedSegment] = []
    tau: list[float] = [0.0]
    t_ready: list[float] = []
    pos = 0
    seg_idx = 0

    while pos < total_frames:
        remaining = total_frames - pos
        lo_f = min(remaining, config.min_segment_frames)
        hi_f = min(remaining, config.max_segment_frames)

        if seg_idx == 0:
            budget = config.first_segment_max_bytes
            t_start = 0.0
        else:
            t_start = max(t_ready[seg_idx - 1], tau[max(0, seg_idx - config.prefetch_depth)])
            avail_s = max(0.0, tau[seg_idx] - config.safety_margin_s - t_start)
            prefetch_budget = int(avail_s * config.prefetch_rate_bps)
            budget = min(config.segment_max_bytes, max(config.min_segment_budget_bytes, prefetch_budget))

        count, blob = max_fit_frames(pos, budget, lo_f, hi_f)

        leftover = total_frames - (pos + count)
        if 0 < leftover < config.min_segment_frames and (count + leftover) <= config.max_segment_frames:
            merged_blob = enc_cached(pos, count + leftover)
            merge_cap = min(config.segment_max_bytes, int(budget * config.tail_merge_slack))
            if len(merged_blob) <= merge_cap:
                count += leftover
                blob = merged_blob

        seg = make_segment(pos, count, blob)
        segments_list.append(seg)
        tau.append(tau[-1] + (seg.duration_ms / 1000.0))
        if seg_idx == 0:
            t_ready.append(0.0)
        else:
            t_ready.append(t_start + (seg.size_bytes / config.prefetch_rate_bps))
        pos += count
        seg_idx += 1

    single_rep = _simulate_segments(segments_list, config, loop=False)
    loop_rep = _simulate_segments(segments_list, config, loop=True)

    # Loop wrap-around protection: if the final segment is too short to cover
    # prefetching seg 0 when looping, search a better split across the last two segments.
    if loop and len(segments_list) >= 2 and not loop_rep.stall_free:
        prev_seg = segments_list[-2]
        last_seg = segments_list[-1]
        combined_start = prev_seg.start_frame
        combined_count = prev_seg.frame_count + last_seg.frame_count
        min_side = min(config.min_segment_frames, combined_count // 2)
        if combined_count >= min_side * 2:
            best_pair: tuple[EncodedSegment, EncodedSegment] | None = None
            best_single = single_rep
            best_loop = loop_rep
            for split_left in range(min_side, combined_count - min_side + 1):
                split_right = combined_count - split_left
                if split_left > config.max_segment_frames or split_right > config.max_segment_frames:
                    continue
                b_left = enc_cached(combined_start, split_left)
                b_right = enc_cached(combined_start + split_left, split_right)
                if len(b_left) > config.segment_max_bytes or len(b_right) > config.segment_max_bytes:
                    continue
                cand_left = make_segment(combined_start, split_left, b_left)
                cand_right = make_segment(combined_start + split_left, split_right, b_right)
                cand_list = segments_list[:-2] + [cand_left, cand_right]
                cand_single = _simulate_segments(cand_list, config, loop=False)
                cand_loop = _simulate_segments(cand_list, config, loop=True)
                if single_rep.stall_free and not cand_single.stall_free:
                    continue
                if cand_loop.min_margin_ms > best_loop.min_margin_ms:
                    best_pair = (cand_left, cand_right)
                    best_single = cand_single
                    best_loop = cand_loop
            if best_pair is not None:
                segments_list[-2], segments_list[-1] = best_pair
                single_rep = best_single
                loop_rep = best_loop

    return SegmentationResult(
        segments=tuple(segments_list),
        single_pass_report=single_rep,
        loop_report=loop_rep,
        encode_calls=encode_calls,
    )


def pack_segmented_animpack_v3(
    *,
    width: int,
    height: int,
    loop: bool,
    default_delay_ms: int,
    frame_delays_ms: Sequence[int],
    segments: Sequence[EncodedSegment],
    swapped_rgb565: bool = True,
) -> bytes:
    """Serialize segmented EFC payloads into an ANPK v3 binary container."""
    frame_count = len(frame_delays_ms)
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if frame_count <= 0:
        raise ValueError("frame_delays_ms must not be empty")
    if not segments:
        raise ValueError("segments must not be empty")

    toc_offset = PACK_HEADER_SIZE
    payload_offset = toc_offset + frame_count * FRAME_DESC_SIZE
    frame_data_size = width * height * 2

    flags = ANIM_FRAME_FLAG_EFC | (ANIM_FRAME_FLAG_EFC_SWAPPED if swapped_rgb565 else 0)
    payload = bytearray()
    toc = bytearray()
    expected_frame = 0

    for seg in segments:
        if seg.start_frame != expected_frame or seg.frame_count <= 0:
            raise ValueError(
                f"Non-contiguous segment coverage: expected start_frame={expected_frame}, "
                f"got start_frame={seg.start_frame} count={seg.frame_count}"
            )
        if not seg.payload:
            raise ValueError(f"Empty segment payload at frame {seg.start_frame}")
        seg_off = len(payload)
        seg_size = len(seg.payload)
        payload.extend(seg.payload)
        for frame_idx in range(seg.start_frame, seg.start_frame + seg.frame_count):
            toc.extend(struct.pack(FRAME_DESC_FMT, seg_off, seg_size, int(frame_delays_ms[frame_idx]), flags))
        expected_frame += seg.frame_count

    if expected_frame != frame_count:
        raise ValueError(f"Segments cover {expected_frame} frames, expected {frame_count}")

    header = struct.pack(
        PACK_HEADER_FMT,
        ANIM_PACK_MAGIC,
        ANIM_PACK_VERSION_EFC,
        width,
        height,
        frame_count,
        1 if loop else 0,
        0,
        int(default_delay_ms),
        toc_offset,
        payload_offset,
        frame_data_size,
    )
    return header + bytes(toc) + bytes(payload)
