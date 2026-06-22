"""
Palm detector using Hailo8L palm_detection_lite.hef
Wraps the blaze_app_python BlazeDetector directly — reuses proven decoder.
"""

import sys
import os
import numpy as np
import cv2
import logging

log = logging.getLogger("palm_detector")

PALM_INPUT_SIZE  = 192
PALM_HEF         = "resources/palm_detection_lite.hef"
BLAZE_APP_PATH   = os.path.expanduser("~/blaze_app")

# Add blaze_app paths so we can import their modules
for _p in [
    os.path.join(BLAZE_APP_PATH, "blaze_common"),
    os.path.join(BLAZE_APP_PATH, "blaze_hailo"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


class HailoInferSimple:
    """Minimal hailo_inference wrapper compatible with BlazeDetector.
    Uses same pattern as hailo_inference_server (no activate(), no scheduler conflict).
    """

    def __init__(self, vdevice):
        from hailo_platform import (HEF, ConfigureParams, FormatType,
                                    HailoStreamInterface, InferVStreams,
                                    InputVStreamParams, OutputVStreamParams)
        self._HEF              = HEF
        self._ConfigureParams  = ConfigureParams
        self._FormatType       = FormatType
        self._Interface        = HailoStreamInterface
        self._IVS              = InferVStreams
        self._InpParams        = InputVStreamParams
        self._OutParams        = OutputVStreamParams
        self.vdevice           = vdevice

        self.hef_list                   = []
        self.network_group_list         = []
        self.network_group_params_list  = []
        self.input_vstreams_params_list = []
        self.output_vstreams_params_list= []

    def load_model(self, model_path):
        hef = self._HEF(model_path)
        cfg = self._ConfigureParams.create_from_hef(hef, self._Interface.PCIe)
        ng  = self.vdevice.configure(hef, cfg)[0]
        ng_params = ng.create_params()
        inp = self._InpParams.make(ng, format_type=self._FormatType.UINT8)
        out = self._OutParams.make(ng, format_type=self._FormatType.FLOAT32)

        hef_id = len(self.hef_list)
        self.hef_list.append(hef)
        self.network_group_list.append(ng)
        self.network_group_params_list.append(ng_params)
        self.input_vstreams_params_list.append(inp)
        self.output_vstreams_params_list.append(out)
        return hef_id


class PalmDetector:
    """
    Two-stage hand pipeline:
      1. palm_detection_lite  — finds palm bbox in full frame
      2. (optional) crop      — returns cropped hand region for landmark model

    Based on blaze_app_python by AlbertaBeef (Mario Bergeron):
    https://github.com/AlbertaBeef/blaze_app_python
    License: see blaze_app_python repository.
    """

    def __init__(self, vdevice, hef_path=PALM_HEF):
        self._hailo_infer = HailoInferSimple(vdevice)

        from blazedetector import BlazeDetector
        from hailo_platform import InferVStreams

        self._det = BlazeDetector("blazepalm", self._hailo_infer)
        self._det.load_model(hef_path)

        # Monkey-patch predict_on_batch to remove activate() call
        # which conflicts with ROUND_ROBIN scheduler
        _infer_simple = self._hailo_infer
        _IVS = InferVStreams

        def _predict_on_batch_no_activate(self_det, x):
            import numpy as np
            from timeit import default_timer as timer

            assert x.shape[3] == 3
            assert x.shape[1] == self_det.y_scale
            assert x.shape[2] == self_det.x_scale

            x = self_det.preprocess(x)
            inp_name = self_det.input_vstream_infos[0].name
            input_data = {inp_name: x}

            ng  = self_det.network_group
            inp = self_det.input_vstreams_params
            out = self_det.output_vstreams_params

            with _IVS(ng, inp, out) as pipe:
                infer_results = pipe.infer(input_data)

            # Decode palm_detection_lite outputs:
            # idx 0: conv24  (12,12,6)   = small scale scores
            # idx 1: conv29  (24,24,2)   = large scale scores
            # idx 2: conv25  (12,12,108) = small scale boxes
            # idx 3: conv30  (24,24,36)  = large scale boxes
            conv_24_24_2   = infer_results[self_det.output_vstream_infos[1].name]  # scores large
            conv_12_12_6   = infer_results[self_det.output_vstream_infos[0].name]  # scores small
            conv_24_24_36  = infer_results[self_det.output_vstream_infos[3].name]  # boxes large
            conv_12_12_108 = infer_results[self_det.output_vstream_infos[2].name]  # boxes small

            out1 = np.concatenate([
                conv_24_24_2.reshape(1, 1152, 1),
                conv_12_12_6.reshape(1, 864, 1),
            ], axis=1).astype(np.float32)

            out2 = np.concatenate([
                conv_24_24_36.reshape(1, 1152, 18),
                conv_12_12_108.reshape(1, 864, 18),
            ], axis=1).astype(np.float32)

            detections = self_det._tensors_to_detections(out2, out1, self_det.anchors)
            filtered = []
            for i in range(len(detections)):
                nms = self_det._weighted_non_max_suppression(detections[i])
                if len(nms) > 0:
                    filtered.append(nms)
            return filtered

        import types
        self._det.predict_on_batch = types.MethodType(
            _predict_on_batch_no_activate, self._det)

        log.info("PalmDetector loaded: %s", hef_path)

    def detect(self, frame: np.ndarray) -> list:
        """
        Detect palms in frame (any size, RGB).
        Returns list of dicts: {score, x1, y1, x2, y2} normalised 0-1.
        """
        img, scale, pad = self._det.resize_pad(frame)
        detections = self._det.predict_on_image(img)
        if len(detections) == 0:
            return []

        detections = np.array(detections)
        detections = self._det.denormalize_detections(detections, scale, pad)

        H, W = frame.shape[:2]
        results = []
        for d in detections:
            # d = [ymin, xmin, ymax, xmax, kp..., score]
            y1, x1, y2, x2 = d[0]/H, d[1]/W, d[2]/H, d[3]/W
            # Score is already sigmoid'd by _tensors_to_detections
            score = float(np.clip(d[self._det.num_coords], 0.0, 1.0))
            results.append({
                'score': score,
                'x1': float(np.clip(x1, 0, 1)),
                'y1': float(np.clip(y1, 0, 1)),
                'x2': float(np.clip(x2, 0, 1)),
                'y2': float(np.clip(y2, 0, 1)),
            })
        return results

    def crop_hand(self, frame: np.ndarray, palm: dict,
                  target_size: int = 224, padding: float = 0.3) -> np.ndarray:
        """Crop and resize hand region for landmark model."""
        H, W = frame.shape[:2]
        x1 = max(0, int((palm['x1'] - (palm['x2']-palm['x1'])*padding) * W))
        y1 = max(0, int((palm['y1'] - (palm['y2']-palm['y1'])*padding) * H))
        x2 = min(W, int((palm['x2'] + (palm['x2']-palm['x1'])*padding) * W))
        y2 = min(H, int((palm['y2'] + (palm['y2']-palm['y1'])*padding) * H))
        if x2 <= x1 or y2 <= y1:
            return cv2.resize(frame, (target_size, target_size))
        crop = frame[y1:y2, x1:x2]
        return cv2.resize(crop, (target_size, target_size),
                          interpolation=cv2.INTER_LINEAR)
