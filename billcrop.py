# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Barros
"""Find a bill (or photo) on the FF-640's grey background, straighten it and crop it.

The scanner returns a fixed-width strip: grey background around the item, and
when the item is shorter than the scan window the backend pads the rest with
pure white. We trim the padding and separate the item from the grey by colour.

A bill that feeds crooked doesn't come out as a neatly rotated rectangle: it
also drifts sideways while it feeds, so the scan is slightly slanted (a
parallelogram). So instead of fitting one rotated rectangle, we fit a straight
line to each of the four edges (using only the middle of each edge, so torn or
dog-eared corners don't throw it off), intersect them to get the four corners,
and map those corners onto an upright rectangle.
"""

import cv2
import numpy as np


def _trim_white_padding(img):
    """Drop the all-white rows the backend appends after the paper ends."""
    not_white = (img < 250).any(axis=2).any(axis=1)
    rows = np.flatnonzero(not_white)
    if rows.size == 0:
        return img
    return img[: rows[-1] + 1]


def _edge_band(w):
    """Columns at the far left/right to ignore: with overscan the scanner
    returns a few dark columns outside its sensor area."""
    return max(2, int(w * 0.025))


def _background_colour(img):
    """Median colour of strips just inside the left/right edges (always
    background: the feed is wider than a bill and items are centre-fed)."""
    w = img.shape[1]
    a = _edge_band(w)
    b = a + max(4, w // 100)
    edges = np.concatenate([img[:, a:b].reshape(-1, 3), img[:, w - b:w - a].reshape(-1, 3)])
    return np.median(edges, axis=0)


def _item_mask(img, min_area_frac=0.02):
    """Filled mask of the largest non-background blob, plus its contour."""
    h, w = img.shape[:2]
    bg = _background_colour(img).astype(np.float32)
    diff = np.abs(img.astype(np.float32) - bg).max(axis=2)
    mask = (diff > 22).astype(np.uint8) * 255
    band = _edge_band(w)
    mask[:, :band] = 0
    mask[:, w - band:] = 0

    k = max(3, w // 300)
    # Pad with background first: OpenCV's erosion treats outside-the-image as
    # foreground, which smears a bill whose corner touches the top edge (the
    # scan starts right where the paper starts) along that edge.
    p = k * 5
    mask = cv2.copyMakeBorder(mask, p, p, p, p, cv2.BORDER_CONSTANT, value=0)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k * 5, k * 5), np.uint8))
    mask = mask[p:-p, p:-p]

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, None
    c = max(contours, key=cv2.contourArea)
    if cv2.contourArea(c) < min_area_frac * h * w:
        return None, None
    filled = np.zeros_like(mask)
    cv2.drawContours(filled, [c], -1, 255, thickness=cv2.FILLED)
    return filled > 0, c


def find_item(img):
    """Return the item's rotated rect ((cx, cy), (w, h), angle) or None."""
    _, c = _item_mask(img)
    return None if c is None else cv2.minAreaRect(c)


def _robust_line(t, v):
    """Fit v = a*t + b, dropping outliers (wavy or nicked edge spots).
    Returns (a, b) or None."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    if t.size < 10:
        return None
    keep = np.ones(t.size, bool)
    for _ in range(3):
        a, b = np.polyfit(t[keep], v[keep], 1)
        res = np.abs(v - (a * t + b))
        mad = np.median(res[keep]) + 1e-6
        new_keep = res <= max(2.0, 3.0 * mad)
        if new_keep.sum() < 10 or (new_keep == keep).all():
            break
        keep = new_keep
    return a, b


def find_corners(img):
    """Return the item's four corners (TL, TR, BR, BL) as float32 (4, 2),
    or None if the edges can't be fitted."""
    mask, _ = _item_mask(img)
    if mask is None:
        return None
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    bw, bh = x1 - x0, y1 - y0

    # Top/bottom edges: sample columns across the middle 80% of the width.
    cols = np.arange(int(x0 + 0.1 * bw), int(x1 - 0.1 * bw), 3)
    colmask = mask[:, cols]
    has = colmask.any(axis=0)
    top = np.argmax(colmask, axis=0)
    bot = h - 1 - np.argmax(colmask[::-1], axis=0)
    # Drop samples sitting on the image border: there the bill was cut off by
    # the scan window, so the real edge is somewhere beyond it.
    t_ok = has & (top > 0)
    b_ok = has & (bot < h - 1)

    # Left/right edges: sample rows across the middle 70% of the height.
    rows = np.arange(int(y0 + 0.15 * bh), int(y1 - 0.15 * bh), 2)
    rowmask = mask[rows, :]
    rhas = rowmask.any(axis=1)
    left = np.argmax(rowmask, axis=1)
    right = w - 1 - np.argmax(rowmask[:, ::-1], axis=1)
    l_ok = rhas & (left > 0)
    r_ok = rhas & (right < w - 1)

    top_l = _robust_line(cols[t_ok], top[t_ok])        # y = a*x + b
    bot_l = _robust_line(cols[b_ok], bot[b_ok])
    left_l = _robust_line(rows[l_ok], left[l_ok])      # x = a*y + b
    right_l = _robust_line(rows[r_ok], right[r_ok])
    if left_l is None or right_l is None:
        return None
    # A long edge that's entirely cut off: assume it's parallel to the other
    # one and lies on the image border (the best we can do; what's past the
    # border was never scanned).
    if top_l is None and bot_l is None:
        return None
    if top_l is None:
        top_l = (bot_l[0], 0.0 - bot_l[0] * (x0 + x1) / 2)
    if bot_l is None:
        bot_l = (top_l[0], (h - 1) - top_l[0] * (x0 + x1) / 2)

    def meet(hl, vl):
        # y = ah*x + bh ; x = av*y + bv
        ah, bh_ = hl
        av, bv = vl
        y = (ah * bv + bh_) / (1 - ah * av)
        return av * y + bv, y

    tl = meet(top_l, left_l)
    tr = meet(top_l, right_l)
    br = meet(bot_l, right_l)
    bl = meet(bot_l, left_l)
    return np.array([tl, tr, br, bl], np.float32)


def _rotated_rect_crop(img, margin_px, bg):
    """Fallback: crop the blob's minimum-area rotated rectangle."""
    rect = find_item(img)
    if rect is None:
        return None
    (cx, cy), (rw, rh), angle = rect
    if rw < rh:
        rw, rh = rh, rw
        angle += 90
    while angle > 45:
        angle -= 90
    while angle < -45:
        angle += 90
    out_w = int(round(rw)) + 2 * margin_px
    out_h = int(round(rh)) + 2 * margin_px
    if out_w <= 0 or out_h <= 0:
        return None
    m = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    m[0, 2] += out_w / 2 - cx
    m[1, 2] += out_h / 2 - cy
    return cv2.warpAffine(img, m, (out_w, out_h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=bg)


def _corners_plausible(q, img):
    """Reject fits that came out twisted or far off the blob."""
    tl, tr, br, bl = q
    wt, wb = np.linalg.norm(tr - tl), np.linalg.norm(br - bl)
    hl, hr = np.linalg.norm(bl - tl), np.linalg.norm(br - tr)
    if min(wt, wb, hl, hr) < 20:
        return False
    if abs(wt - wb) > 0.05 * max(wt, wb) or abs(hl - hr) > 0.08 * max(hl, hr):
        return False
    rect = find_item(img)
    if rect is None:
        return False
    area_rect = rect[1][0] * rect[1][1]
    area_quad = cv2.contourArea(q.reshape(-1, 1, 2))
    return 0.85 * area_rect <= area_quad <= 1.15 * area_rect


def crop_item(img, margin_px=0):
    """Return (cropped_image, found). If nothing is found, returns the
    padding-trimmed scan unchanged and found=False.

    margin_px > 0 keeps that much background around the item, so a slightly
    misjudged edge is kept rather than trimmed; margin_px < 0 trims inward."""
    img = _trim_white_padding(img)
    bg = tuple(float(v) for v in _background_colour(img))

    q = find_corners(img)
    if q is not None and _corners_plausible(q, img):
        tl, tr, br, bl = q
        out_w = int(round((np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2))
        out_h = int(round((np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2))
        m = margin_px
        dst = np.array([[m, m], [m + out_w - 1, m], [m + out_w - 1, m + out_h - 1],
                        [m, m + out_h - 1]], np.float32)
        H = cv2.getPerspectiveTransform(q, dst)
        # Anything outside the scan is filled with the background grey (not
        # smeared edge pixels), so a cut-off edge looks like what it is.
        out = cv2.warpPerspective(img, H, (out_w + 2 * m, out_h + 2 * m),
                                  flags=cv2.INTER_CUBIC,
                                  borderMode=cv2.BORDER_CONSTANT, borderValue=bg)
        return out, True

    out = _rotated_rect_crop(img, margin_px, bg)
    if out is None:
        return img, False
    return out, True


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:]:
        im = cv2.imread(path)
        out, ok = crop_item(im)
        dst = path.rsplit(".", 1)[0] + "_crop.jpg"
        cv2.imwrite(dst, out, [cv2.IMWRITE_JPEG_QUALITY, 95])
        print(path, "->", dst, out.shape[1], "x", out.shape[0], "found" if ok else "NOT FOUND")
