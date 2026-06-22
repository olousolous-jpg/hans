"""
BLAZE_PALM_ANCHORS_V1 — SSD anchory pro BlazePalm detekci.

Pro `palm_detection_lite` Hailo model (192×192, 2016 anchorů, MediaPipe BlazePalm
v0.10). Vytaženo z externího `blaze_app/blazeconfig` DO projektu → odstraňuje
závislost na `~/blaze_app`. Čistá numpy implementace, žádné externí importy.

Použití (v hailo_inference_server.run_palm):
    from scripts.blaze_palm_anchors import palm_anchors
    anchors = palm_anchors()        # (2016, 4): [x_center, y_center, w, h]
"""

import numpy as np

# MediaPipe BlazePalm v0.10 (192px, 2016 anchorů) — z blaze_app/blazeconfig.
_PALM_OPTS = {
    "num_layers": 4,
    "strides": [8, 16, 16, 16],
    "input_size_width": 192,
    "input_size_height": 192,
    "anchor_offset_x": 0.5,
    "anchor_offset_y": 0.5,
    "min_scale": 0.1484375,
    "max_scale": 0.75,
    "aspect_ratios": [1.0],
    "fixed_anchor_size": True,
    "interpolated_scale_aspect_ratio": 1.0,
    "reduce_boxes_in_lowest_layer": False,
}


def _calc_scale(min_scale, max_scale, stride_index, num_strides):
    if num_strides == 1:
        return (max_scale + min_scale) * 0.5
    return min_scale + (max_scale - min_scale) * stride_index / (num_strides - 1.0)


def generate_anchors(options):
    """SSD anchor generátor (MediaPipe). Vrací np.array (N,4) [cx, cy, w, h]."""
    strides_size = len(options["strides"])
    assert options["num_layers"] == strides_size

    anchors = []
    layer_id = 0
    while layer_id < strides_size:
        anchor_height = []
        anchor_width = []
        aspect_ratios = []
        scales = []

        last_same_stride_layer = layer_id
        while (last_same_stride_layer < strides_size) and \
              (options["strides"][last_same_stride_layer] == options["strides"][layer_id]):
            scale = _calc_scale(options["min_scale"], options["max_scale"],
                                last_same_stride_layer, strides_size)

            if last_same_stride_layer == 0 and options["reduce_boxes_in_lowest_layer"]:
                aspect_ratios += [1.0, 2.0, 0.5]
                scales += [0.1, scale, scale]
            else:
                for aspect_ratio in options["aspect_ratios"]:
                    aspect_ratios.append(aspect_ratio)
                    scales.append(scale)
                if options["interpolated_scale_aspect_ratio"] > 0.0:
                    scale_next = 1.0 if last_same_stride_layer == strides_size - 1 \
                        else _calc_scale(options["min_scale"], options["max_scale"],
                                         last_same_stride_layer + 1, strides_size)
                    scales.append(np.sqrt(scale * scale_next))
                    aspect_ratios.append(options["interpolated_scale_aspect_ratio"])
            last_same_stride_layer += 1

        for i in range(len(aspect_ratios)):
            ratio_sqrts = np.sqrt(aspect_ratios[i])
            anchor_height.append(scales[i] / ratio_sqrts)
            anchor_width.append(scales[i] * ratio_sqrts)

        stride = options["strides"][layer_id]
        fm_h = int(np.ceil(options["input_size_height"] / stride))
        fm_w = int(np.ceil(options["input_size_width"] / stride))

        for y in range(fm_h):
            for x in range(fm_w):
                for anchor_id in range(len(anchor_height)):
                    x_center = (x + options["anchor_offset_x"]) / fm_w
                    y_center = (y + options["anchor_offset_y"]) / fm_h
                    if options["fixed_anchor_size"]:
                        new_anchor = [x_center, y_center, 1.0, 1.0]
                    else:
                        new_anchor = [x_center, y_center,
                                      anchor_width[anchor_id], anchor_height[anchor_id]]
                    anchors.append(new_anchor)
        layer_id = last_same_stride_layer

    return np.asarray(anchors)


_cache = None


def palm_anchors():
    """(2016, 4) anchory pro BlazePalm 192px. Cachované (generuje se 1×)."""
    global _cache
    if _cache is None:
        _cache = generate_anchors(_PALM_OPTS)
    return _cache


if __name__ == "__main__":
    a = palm_anchors()
    print("anchors shape:", a.shape, "(očekáváno (2016, 4))")
