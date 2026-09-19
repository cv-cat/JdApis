"""Local image solver for the JCAP challenge types used by SMS login.

The challenge image is read as bytes from stdin.  Only numeric solution data is
written to stdout, so the image and session values stay out of process logs.
"""

from __future__ import annotations

import base64
import heapq
import json
import math
import os
from pathlib import Path
import sys

import cv2
import numpy as np
import onnxruntime as ort
from scipy.optimize import differential_evolution
from skimage.morphology import skeletonize


def _sample_color(image: np.ndarray, points: np.ndarray) -> np.ndarray:
    return cv2.remap(
        image,
        points[:, 0].astype(np.float32),
        points[:, 1].astype(np.float32),
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT_101,
    ).reshape(-1, image.shape[2])


def _sample_scalar(image: np.ndarray, points: np.ndarray) -> np.ndarray:
    return cv2.remap(
        image,
        points[:, 0].astype(np.float32),
        points[:, 1].astype(np.float32),
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT_101,
    ).reshape(-1)


def _corners(params: np.ndarray) -> np.ndarray:
    (top_x, top_y, top_half_width, top_angle_deg,
     bottom_x, bottom_y, bottom_half_width, bottom_angle_deg) = params

    def endpoints(cx: float, cy: float, half_width: float,
                  angle_deg: float) -> tuple[np.ndarray, np.ndarray]:
        angle = math.radians(angle_deg)
        vector = half_width * np.array([math.cos(angle), math.sin(angle)])
        center = np.array([cx, cy])
        return center - vector, center + vector

    top_left, top_right = endpoints(top_x, top_y, top_half_width, top_angle_deg)
    bottom_left, bottom_right = endpoints(
        bottom_x, bottom_y, bottom_half_width, bottom_angle_deg)
    return np.array([top_left, top_right, bottom_left, bottom_right])


def _trace_chain(params: np.ndarray, topology: tuple[int, int, int, int]) -> np.ndarray:
    return np.asarray(_corners(params)[list(topology)], dtype=np.float32)


def _u2net_saliency(image: np.ndarray, model_path: Path) -> np.ndarray:
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (320, 320), interpolation=cv2.INTER_LANCZOS4).astype(np.float32)
    resized /= max(float(resized.max()), 1e-6)
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    tensor = ((resized - mean) / std).transpose(2, 0, 1)[None].astype(np.float32)
    prediction = session.run(None, {session.get_inputs()[0].name: tensor})[0][:, 0]
    prediction = np.squeeze(prediction)
    prediction -= float(prediction.min())
    prediction /= max(float(prediction.max()), 1e-6)
    return cv2.resize(prediction.astype(np.float32),
                      (image.shape[1], image.shape[0]), interpolation=cv2.INTER_LANCZOS4)


def _stroke_likelihood(lab: np.ndarray) -> np.ndarray:
    """Prefer the center of a broad overlay stroke over thin scene edges."""
    background = cv2.medianBlur(lab.astype(np.uint8), 31).astype(np.float32)
    residual = np.linalg.norm(lab - background, axis=2).astype(np.float32)
    likelihood = np.zeros(residual.shape, dtype=np.float32)
    for threshold in (10.0, 15.0, 20.0, 25.0):
        binary = (residual >= threshold).astype(np.uint8)
        distance = cv2.distanceTransform(binary, cv2.DIST_L2, 5)
        likelihood += np.clip(distance / 8.0, 0.0, 1.0)
    return likelihood / 4.0


def _farthest(graph: dict[tuple[int, int], list[tuple[tuple[int, int], float]]],
              start: tuple[int, int]) -> tuple[tuple[int, int], dict, dict]:
    distances = {start: 0.0}
    parents = {}
    queue = [(0.0, start)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances[node]:
            continue
        for neighbor, weight in graph[node]:
            candidate = distance + weight
            if candidate < distances.get(neighbor, float("inf")):
                distances[neighbor] = candidate
                parents[neighbor] = node
                heapq.heappush(queue, (candidate, neighbor))
    node = max(distances, key=distances.get)
    return node, distances, parents


def _longest_skeleton_path(skeleton: np.ndarray) -> tuple[list[tuple[int, int]], dict[str, float]]:
    pixels = [tuple(point) for point in np.argwhere(skeleton)]
    pixel_set = set(pixels)
    graph = {}
    for y, x in pixels:
        neighbors = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                neighbor = (y + dy, x + dx)
                if not (dx or dy) or neighbor not in pixel_set:
                    continue
                neighbors.append((neighbor, math.sqrt(dx * dx + dy * dy)))
        graph[(y, x)] = neighbors
    if not graph:
        return [], {}
    degrees = np.array([len(neighbors) for neighbors in graph.values()])
    endpoints = [node for node, neighbors in graph.items() if len(neighbors) == 1]
    first_start = endpoints[0] if endpoints else next(iter(graph))
    first, _, _ = _farthest(graph, first_start)
    second, distances, parents = _farthest(graph, first)
    path = [second]
    while path[-1] != first:
        parent = parents.get(path[-1])
        if parent is None:
            break
        path.append(parent)
    path.reverse()
    return path, {
        "pixels": float(len(pixels)),
        "endpoints": float(len(endpoints)),
        "branches": float(np.sum(degrees >= 4)),
        "length": float(distances.get(second, 0.0)),
    }


def _resample_path(path: list[tuple[int, int]], count: int = 64) -> list[list[float]]:
    points = np.array([[x, y] for y, x in path], dtype=np.float32)
    simplified = cv2.approxPolyDP(points.reshape(-1, 1, 2), 1.6, False).reshape(-1, 2)
    if len(simplified) < 2:
        simplified = points
    distances = np.linalg.norm(np.diff(simplified, axis=0), axis=1)
    cumulative = np.concatenate([[0.0], np.cumsum(distances)])
    if cumulative[-1] <= 0:
        return simplified.round(2).tolist()
    targets = np.linspace(0, cumulative[-1], count)
    result = []
    segment = 0
    for target in targets:
        while segment + 1 < len(cumulative) - 1 and cumulative[segment + 1] < target:
            segment += 1
        span = max(cumulative[segment + 1] - cumulative[segment], 1e-6)
        ratio = (target - cumulative[segment]) / span
        point = simplified[segment] + ratio * (simplified[segment + 1] - simplified[segment])
        result.append([round(float(point[0]), 2), round(float(point[1]), 2)])
    return result


def _extract_confident_path(saliency: np.ndarray) -> dict[str, object]:
    height, width = saliency.shape
    best = None
    for threshold in (.88, .82, .76, .70, .64):
        binary = (saliency >= threshold).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
        for label in range(1, count):
            x, y, box_width, box_height, area = stats[label]
            if (area < 250 or box_width < width * .35 or box_height < height * .18):
                continue
            component = labels == label
            path, metrics = _longest_skeleton_path(skeletonize(component))
            if not path:
                continue
            thickness = float(area) / max(metrics["length"], 1.0)
            coverage = metrics["length"] / max(math.hypot(box_width, box_height), 1.0)
            confidence = (metrics["length"] - 7 * metrics["endpoints"]
                          - 2 * metrics["branches"]
                          - 12 * max(0.0, thickness - 18.0))
            center_x = x + box_width / 2
            center_y = y + box_height / 2
            candidate = {
                **metrics,
                "threshold": threshold,
                "area": float(area),
                "width": float(box_width),
                "height": float(box_height),
                "thickness": thickness,
                "coverage": coverage,
                "confidence": confidence,
                "center_ok": width * .22 <= center_x <= width * .78
                and height * .15 <= center_y <= height * .85,
            }
            if best is None or candidate["confidence"] > best[1]["confidence"]:
                best = (path, candidate)
    if best is None:
        return {"retry": True, "reason": "no-path", "score": 0.0}
    path, metrics = best
    accepted = (metrics["confidence"] >= 100 and metrics["length"] >= 150
                and metrics["thickness"] <= 22 and metrics["coverage"] >= 1.05
                and metrics["endpoints"] <= 5 and metrics["center_ok"])
    if not accepted:
        return {
            "retry": True,
            "reason": "low-confidence",
            "score": round(float(metrics["confidence"]), 2),
        }
    return {
        "retry": False,
        "score": round(float(metrics["confidence"]), 2),
        "points": _resample_path(path),
    }


def _orientation_tensor(image: np.ndarray) -> np.ndarray:
    resized = cv2.resize(image, (416, 416), interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(resized[16:400, 16:400], cv2.COLOR_BGR2RGB).astype(np.float32)
    rgb /= 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    return ((rgb - mean) / std).transpose(2, 0, 1)[None].astype(np.float32)


def _circular_distance(first: float, second: float) -> float:
    delta = abs((first - second) % 360.0)
    return min(delta, 360.0 - delta)


def solve_click(image: np.ndarray, tip: np.ndarray,
                model_path: Path) -> dict[str, object]:
    """Locate the single object shown in the tp=2 hint image."""
    saliency = np.clip(_u2net_saliency(tip, model_path), 0.0, 1.0)
    threshold = max(0.25, float(saliency.max()) * 0.35)
    binary = (saliency >= threshold).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    candidates = []
    for index in range(1, count):
        x, y, width, height, area = map(int, stats[index])
        if area < 12 or width < 3 or height < 3:
            continue
        component_score = float(saliency[labels == index].sum())
        candidates.append((component_score, x, y, width, height))
    if not candidates:
        return {"retry": True, "reason": "tip-object-not-found", "score": 0.0}
    _, x, y, width, height = max(candidates)
    padding = 2
    x = max(0, x - padding)
    y = max(0, y - padding)
    width = min(tip.shape[1] - x, width + 2 * padding)
    height = min(tip.shape[0] - y, height + 2 * padding)
    template = tip[y:y + height, x:x + width]
    template_mask = (saliency[y:y + height, x:x + width] >= threshold).astype(
        np.uint8) * 255

    best: tuple[float, tuple[int, int], tuple[int, int]] | None = None
    for scale in np.linspace(0.70, 1.40, 15):
        scaled_width = max(4, int(round(width * float(scale))))
        scaled_height = max(4, int(round(height * float(scale))))
        if scaled_width >= image.shape[1] or scaled_height >= image.shape[0]:
            continue
        scaled = cv2.resize(template, (scaled_width, scaled_height),
                            interpolation=cv2.INTER_CUBIC)
        mask = cv2.resize(template_mask, (scaled_width, scaled_height),
                          interpolation=cv2.INTER_NEAREST)
        scores = cv2.matchTemplate(
            image, scaled, cv2.TM_CCORR_NORMED, mask=mask)
        scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, score, _, location = cv2.minMaxLoc(scores)
        if best is None or score > best[0]:
            best = (float(score), location, (scaled_width, scaled_height))
    if best is None or best[0] < 0.72:
        return {
            "retry": True,
            "reason": "click-match-low-confidence",
            "score": round(best[0] if best else 0.0, 4),
        }
    score, location, size = best
    return {
        "retry": False,
        "solver": "u2net-masked-template",
        "x": round(location[0] + size[0] / 2.0, 2),
        "y": round(location[1] + size[1] / 2.0, 2),
        "score": round(score, 4),
    }


def solve_rotation(image: np.ndarray, model_path: Path) -> dict[str, object]:
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    logits = np.asarray(session.run(
        None, {session.get_inputs()[0].name: _orientation_tensor(image)})[0][0],
        dtype=np.float32)
    logits -= float(logits.max())
    probabilities = np.exp(logits)
    probabilities /= max(float(probabilities.sum()), 1e-6)
    orientation_class = int(np.argmax(probabilities))
    # The model labels are correction actions. OpenCV positive angles are
    # counter-clockwise, while class 1 is a clockwise correction.
    coarse_cv_angle = {0: 0.0, 1: 270.0, 2: 180.0, 3: 90.0}[orientation_class]

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    lines = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD).detect(gray)[0]
    weighted_axis = 0j
    total_weight = 0.0
    if lines is not None:
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            dx = float(x2 - x1)
            dy = float(y2 - y1)
            length = math.hypot(dx, dy)
            if length < 12:
                continue
            theta = math.atan2(dy, dx)
            weight = length * length
            weighted_axis += weight * complex(math.cos(4 * theta), math.sin(4 * theta))
            total_weight += weight
    if total_weight <= 0:
        cv_angle = coarse_cv_angle
        axis_strength = 0.0
    else:
        axis_angle = (math.degrees(math.atan2(
            weighted_axis.imag, weighted_axis.real)) / 4.0) % 90.0
        candidates = [(axis_angle + 90.0 * index) % 360.0 for index in range(4)]
        cv_angle = min(candidates,
                       key=lambda candidate: _circular_distance(candidate, coarse_cv_angle))
        axis_strength = abs(weighted_axis) / total_weight
    css_angle = (360.0 - cv_angle) % 360.0
    return {
        "retry": False,
        "solver": "orientation-classifier-axis",
        "angle": round(css_angle, 2),
        "cvAngle": round(cv_angle, 2),
        "orientationClass": orientation_class,
        "score": round(float(probabilities[orientation_class]), 4),
        "axisStrength": round(float(axis_strength), 4),
    }


def solve_slider(image: np.ndarray, slot: np.ndarray) -> dict[str, object]:
    """Locate the tp=30 puzzle gap using the transparent piece silhouette."""
    if slot.ndim != 3 or slot.shape[2] != 4:
        return {"retry": True, "reason": "slot-alpha-missing", "score": 0.0}
    if slot.shape[0] != image.shape[0] or slot.shape[1] >= image.shape[1]:
        return {"retry": True, "reason": "slot-size-mismatch", "score": 0.0}

    alpha = slot[:, :, 3]
    mask = (alpha >= 48).astype(np.uint8)
    if int(mask.sum()) < 200:
        return {"retry": True, "reason": "slot-mask-empty", "score": 0.0}
    contour = cv2.Canny(mask * 255, 50, 150) > 0
    ys, xs = np.nonzero(contour)
    interior = cv2.erode(mask, np.ones((3, 3), np.uint8), 1) > 0
    interior_y, interior_x = np.nonzero(interior)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gradient_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    gradient = cv2.magnitude(gradient_x, gradient_y)
    edges = cv2.Canny(gray, 45, 120)
    distance = cv2.distanceTransform(
        (edges == 0).astype(np.uint8), cv2.DIST_L2, 3)
    signed_distance = (
        cv2.distanceTransform(mask, cv2.DIST_L2, 3)
        - cv2.distanceTransform(1 - mask, cv2.DIST_L2, 3)
    )
    normal_x = cv2.Sobel(signed_distance, cv2.CV_32F, 1, 0, 3)[ys, xs]
    normal_y = cv2.Sobel(signed_distance, cv2.CV_32F, 0, 1, 3)[ys, xs]
    normal_length = np.maximum(np.hypot(normal_x, normal_y), 1e-6)
    normal_x /= normal_length
    normal_y /= normal_length
    slot_gray = cv2.cvtColor(slot[:, :, :3], cv2.COLOR_BGR2GRAY).astype(
        np.float32)[interior_y, interior_x]
    slot_gray -= float(slot_gray.mean())
    slot_scale = max(float(np.linalg.norm(slot_gray)), 1e-6)

    scores = []
    correlations = []
    for offset in range(image.shape[1] - slot.shape[1] + 1):
        shifted_x = xs + offset
        contour_distance = distance[ys, shifted_x]
        contour_gradient = gradient[ys, shifted_x]
        alignment = np.abs(
            gradient_x[ys, shifted_x] * normal_x
            + gradient_y[ys, shifted_x] * normal_y
        ) / np.maximum(contour_gradient, 1e-6)
        chamfer = float(np.mean(np.exp(-contour_distance / 1.7)))
        coverage = float(np.mean(contour_distance <= 2.2))
        oriented = float(np.mean(
            np.clip(contour_gradient / 500.0, 0.0, 1.0) * alignment))

        # JCAP leaves the original texture visible beneath a gray gap overlay.
        # Normalized correlation with the cut-out rejects high-contrast decoy
        # contours while remaining invariant to that brightness change.
        candidate = gray[interior_y, interior_x + offset].astype(np.float32)
        candidate -= float(candidate.mean())
        correlation = float(np.dot(slot_gray, candidate) / (
            slot_scale * max(float(np.linalg.norm(candidate)), 1e-6)))
        score = (1.2 * chamfer + 0.9 * coverage + 0.55 * oriented
                 + 0.9 * max(correlation, 0.0))
        scores.append(score)
        correlations.append(correlation)
    order = np.argsort(np.asarray(scores))[::-1]
    best_index = int(order[0])
    separated = [int(index) for index in order if abs(int(index) - best_index) >= 8]
    runner_up = scores[separated[0]] if separated else 0.0
    margin = float(scores[best_index] - runner_up)
    accepted = scores[best_index] >= 1.9 and correlations[best_index] >= 0.6
    return {
        "retry": not accepted,
        "reason": "low-slider-confidence" if not accepted else "",
        "solver": "slider-contour-texture",
        "offset": round(float(best_index), 2),
        "score": round(float(scores[best_index]), 4),
        "margin": round(margin, 4),
        "correlation": round(float(correlations[best_index]), 4),
    }


def _trace_score(lab: np.ndarray, residual: np.ndarray, texture: np.ndarray,
                 saliency: np.ndarray, params: np.ndarray,
                 topology: tuple[int, int, int, int]) -> float:
    height, width = lab.shape[:2]
    chain = _trace_chain(params, topology)
    if (chain[:, 0].min() < 4 or chain[:, 1].min() < 4
            or chain[:, 0].max() >= width - 4 or chain[:, 1].max() >= height - 4):
        return -1e3
    source_corners = _corners(params)
    if float(source_corners[:2, 1].max()) + 18 >= float(source_corners[2:, 1].min()):
        return -1e3
    if (float(source_corners[:2, 1].max()) > height * .43
            or float(source_corners[2:, 1].min()) < height * .45
            or float(source_corners[3, 1]) > height * .72):
        return -1e3
    if (float(source_corners[[0, 2], 0].max()) > width * .45
            or float(source_corners[[1, 3], 0].min()) < width * .65):
        return -1e3
    if abs(float(source_corners[1, 0] - source_corners[3, 0])) > width * .12:
        return -1e3

    # A generated four-point trace has a real bend at both internal vertices.
    # Without this guard an optimizer can place the fourth point back on the
    # preceding stroke, score the same pixels twice and silently omit a faint
    # final segment.
    vectors = np.diff(chain, axis=0)
    if float(np.linalg.norm(vectors, axis=1).sum()) < width * .98:
        return -1e3
    turn_penalty = 0.0
    for incoming, outgoing in zip(vectors[:-1], vectors[1:]):
        denominator = max(float(np.linalg.norm(incoming) * np.linalg.norm(outgoing)), 1e-6)
        cosine = float(np.clip(np.dot(incoming, outgoing) / denominator, -1.0, 1.0))
        angle = math.degrees(math.acos(cosine))
        if angle < 28.0:
            turn_penalty += (28.0 - angle) * 4.0
        elif angle > 152.0:
            turn_penalty += (angle - 152.0) * 4.0

    segment_scores = []
    for start, end in zip(chain[:-1], chain[1:]):
        vector = end - start
        length = np.linalg.norm(vector)
        if length < 35:
            return -1e3
        unit = vector / length
        normal = np.array([-unit[1], unit[0]])
        ratios = np.linspace(.02, .98, max(12, int(length / 2.5)))
        centers = start[None, :] + ratios[:, None] * vector[None, :]

        best_edge_score = -1e3
        for radius in (5.0, 7.0, 9.0, 11.0, 13.0):
            inner = max(1.0, radius - 2.0)
            outer = radius + 4.0
            left = np.linalg.norm(
                _sample_color(lab, centers + normal * inner)
                - _sample_color(lab, centers + normal * outer), axis=1)
            right = np.linalg.norm(
                _sample_color(lab, centers - normal * inner)
                - _sample_color(lab, centers - normal * outer), axis=1)
            edge_score = float(np.mean(np.clip(np.minimum(left, right), 0, 45)))
            best_edge_score = max(best_edge_score, edge_score)

        center_residual = np.max(np.stack([
            _sample_scalar(residual, centers + normal * offset)
            for offset in (-4.0, 0.0, 4.0)
        ]), axis=0)
        residual_score = float(np.mean(np.clip(center_residual, 0, 45)))

        center_texture = _sample_scalar(texture, centers)
        outside_texture = .5 * (
            _sample_scalar(texture, centers + normal * 15.0)
            + _sample_scalar(texture, centers - normal * 15.0))
        texture_score = float(np.mean(np.clip(
            outside_texture - center_texture, -30, 30)))

        center_saliency = _sample_scalar(saliency, centers)
        outside_saliency = .5 * (
            _sample_scalar(saliency, centers + normal * 16.0)
            + _sample_scalar(saliency, centers - normal * 16.0))
        saliency_score = float(np.mean(np.clip(
            center_saliency - outside_saliency, -.45, 1.0))) * 45.0

        segment_scores.append(
            best_edge_score + 1.15 * residual_score
            + .75 * texture_score + 1.35 * saliency_score)
    return float(np.mean(segment_scores) - turn_penalty)


def solve_trace(image: np.ndarray, model_path: Path) -> dict[str, object]:
    saliency = _u2net_saliency(image, model_path)
    skeleton_solution = _extract_confident_path(saliency)
    # U2Net can truncate a translucent final segment.  Use it only as one of
    # the likelihood maps and fit the complete four-vertex generator shape.
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    background = cv2.medianBlur(lab.astype(np.uint8), 31).astype(np.float32)
    residual = np.linalg.norm(lab - background, axis=2).astype(np.float32)
    stroke_likelihood = _stroke_likelihood(lab)
    geometric_saliency = .25 * saliency + .75 * stroke_likelihood
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gradient_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    texture = cv2.GaussianBlur(cv2.magnitude(gradient_x, gradient_y), (0, 0), 3)

    height, width = image.shape[:2]
    bounds = [
        (width * .32, width * .68),
        (height * .10, height * .48),
        (width * .12, width * .43),
        (-35, 35),
        (width * .32, width * .68),
        (height * .45, height * .90),
        (width * .12, width * .43),
        (-35, 35),
    ]
    candidates = []
    for topology in ((0, 1, 2, 3), (0, 2, 1, 3)):
        result = differential_evolution(
            lambda params: -_trace_score(
                lab, residual, texture, geometric_saliency, params, topology),
            bounds,
            seed=17,
            popsize=8,
            maxiter=32,
            polish=True,
            workers=1,
        )
        candidates.append({
            "score": -float(result.fun),
            "topology": topology,
            "chain": _trace_chain(result.x, topology),
        })
    geometric = max(candidates, key=lambda candidate: candidate["score"])
    if geometric["score"] < 55.0:
        return {
            "retry": True,
            "reason": "low-geometric-confidence",
            "score": round(float(geometric["score"]), 2),
            "saliencyScore": skeleton_solution.get("score", 0.0),
        }
    chain = np.asarray(geometric["chain"], dtype=np.float32)
    skeleton_chain = [(int(round(y)), int(round(x))) for x, y in chain]
    return {
        "retry": False,
        "solver": "four-corner-geometric",
        "score": round(float(geometric["score"]), 2),
        "points": _resample_path(skeleton_chain),
    }


def _options(argv):
    """Parse the private Node-to-Python key/value protocol."""
    values = {}
    index = 0
    while index < len(argv):
        key = argv[index]
        if not key.startswith("--") or index + 1 >= len(argv):
            raise SystemExit("invalid solver arguments")
        values[key[2:]] = argv[index + 1]
        index += 2
    if "tp" not in values:
        raise SystemExit("missing challenge type")
    return values


def main() -> int:
    options = _options(sys.argv[1:])
    challenge_type = int(options["tp"])
    encoded = sys.stdin.buffer.read()
    image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit("invalid challenge image")
    if challenge_type in (2, 3):
        default_model = (Path(__file__).resolve().parent / "models"
                         / "u2netp.onnx")
        model_path = Path(options.get("model") or os.getenv(
            "JCAP_U2NETP_MODEL", default_model))
        if not model_path.is_file():
            raise SystemExit(f"missing saliency model: {model_path}")
        if challenge_type == 2:
            try:
                tip_bytes = base64.b64decode(
                    options.get("tip-base64") or "", validate=True)
            except (ValueError, TypeError) as exc:
                raise SystemExit("invalid tip image") from exc
            tip = cv2.imdecode(np.frombuffer(tip_bytes, dtype=np.uint8),
                               cv2.IMREAD_COLOR)
            if tip is None:
                raise SystemExit("invalid tip image")
            solution = solve_click(image, tip, model_path)
        else:
            solution = solve_trace(image, model_path)
    elif challenge_type == 26:
        default_orientation_model = (Path(__file__).resolve().parent / "models"
                                     / "orientation_model_v2_0.9882.onnx")
        orientation_model = Path(options.get("orientation-model") or os.getenv(
            "JCAP_ORIENTATION_MODEL", default_orientation_model))
        if not orientation_model.is_file():
            raise SystemExit(f"missing orientation model: {orientation_model}")
        solution = solve_rotation(image, orientation_model)
    elif challenge_type == 30:
        try:
            slot_bytes = base64.b64decode(
                options.get("slot-base64") or "", validate=True)
        except (ValueError, TypeError) as exc:
            raise SystemExit("invalid slot image") from exc
        slot = cv2.imdecode(np.frombuffer(slot_bytes, dtype=np.uint8),
                            cv2.IMREAD_UNCHANGED)
        if slot is None:
            raise SystemExit("invalid slot image")
        solution = solve_slider(image, slot)
    else:
        raise SystemExit(f"unsupported challenge type: {challenge_type}")
    if options.get("debug-output"):
        debug = image.copy()
        points = solution.get("points") or []
        if len(points) >= 2:
            polyline = np.round(np.asarray(points, dtype=np.float32)).astype(np.int32)
            cv2.polylines(debug, [polyline], False, (0, 0, 255), 2, cv2.LINE_AA)
            cv2.circle(debug, tuple(polyline[0]), 4, (0, 255, 0), -1)
            cv2.circle(debug, tuple(polyline[-1]), 4, (255, 0, 0), -1)
        elif "x" in solution and "y" in solution:
            cv2.circle(debug, (round(float(solution["x"])),
                               round(float(solution["y"]))),
                       8, (0, 0, 255), 2, cv2.LINE_AA)
        elif "cvAngle" in solution:
            height, width = debug.shape[:2]
            matrix = cv2.getRotationMatrix2D(
                (width / 2, height / 2), float(solution["cvAngle"]), 1.0)
            debug = cv2.warpAffine(debug, matrix, (width, height),
                                   flags=cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_CONSTANT,
                                   borderValue=(0, 0, 0))
        cv2.imwrite(str(Path(options["debug-output"])), debug)
    print(json.dumps(solution, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
