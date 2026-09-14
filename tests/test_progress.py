from app.main import _ProgressTracker


def _feed_all(lines):
    tk = _ProgressTracker()
    out = []
    for line in lines:
        p = tk.feed(line)
        if p:
            out.append(p)
    return out


def test_single_batch_is_monotonic_and_reaches_85():
    lines = [
        "Pipeline processing-window multi-file run. doc_count=1, total_pages=1, "
        "window_size=64, total_batches=1",
        "model init done",
        "Pipeline processing window batch 1/1: 1/1 pages, batch_pages=1, doc_slices=...",
        "Layout Predict:   0%|          | 0/1 [00:00<?, ?it/s]",
        "Layout Predict: 100%|##########| 1/1 [00:00<?, ?it/s]",
        "Table orientation: 100%|##########| 1/1 [00:00<?, ?it/s]",
        "Table-ocr rec ch: 100%|##########| 24/24 [00:00<?, ?it/s]",
        "OCR-det ch: 100%|##########| 3/3 [00:00<?, ?it/s]",
        "OCR-rec Predict: 100%|##########| 4/4 [00:00<?, ?it/s]",
        "Processing pages: 100%|##########| 1/1 [00:00<?, ?it/s]",
    ]
    out = _feed_all(lines)
    pcts = [p["pct"] for p in out]
    assert pcts[0] == 10
    assert pcts == sorted(pcts)
    assert pcts[-1] == 85
    assert max(pcts) <= 85


def test_stage_bands_layout_table_ocr():
    out = _feed_all([
        "Pipeline processing-window multi-file run. doc_count=1, total_pages=1, "
        "window_size=64, total_batches=1",
        "Layout Predict: 100%|##| 1/1 [00:00<?, ?it/s]",
        "Table orientation: 100%|##| 1/1 [00:00<?, ?it/s]",
        "OCR-rec Predict: 100%|##| 1/1 [00:00<?, ?it/s]",
    ])
    assert [p["pct"] for p in out] == [25, 34, 85]
    assert [p["phase"] for p in out] == [
        "布局分析 1/1 页",
        "表格识别 1/1 区域",
        "文字识别 1/1 区域",
    ]


def test_counts_come_from_current_bar():
    out = _feed_all([
        "Pipeline processing-window multi-file run. doc_count=1, total_pages=10, "
        "window_size=64, total_batches=1",
        "Layout Predict:  50%|##| 5/10 [00:00<?, ?it/s]",
        "OCR-det ch:  12%|##| 16/129 [00:00<?, ?it/s]",
        "Processing pages:  50%|##| 5/10 [00:00<?, ?it/s]",
    ])
    assert out[-3]["phase"] == "布局分析 5/10 页"
    assert out[-2]["phase"] == "文字识别 16/129 区域"
    assert out[-1]["phase"] == "处理页面 5/10 页"


def test_ocr_fills_table_band_when_no_table_stage():
    out = _feed_all([
        "Pipeline processing-window multi-file run. doc_count=1, total_pages=10, "
        "window_size=64, total_batches=1",
        "Layout Predict: 100%|##| 10/10 [00:00<?, ?it/s]",
        "OCR-det ch:   0%|##| 0/129 [00:00<?, ?it/s]",
        "OCR-det ch: 100%|##| 129/129 [00:00<?, ?it/s]",
        "OCR-rec Predict: 100%|##| 40/40 [00:00<?, ?it/s]",
    ])
    pcts = [p["pct"] for p in out]
    assert pcts[0] == 25            # layout 结束
    assert pcts[1] == 25            # OCR 无表格时从 layout 结束处起步
    assert pcts[2] == 73            # OCR-det 占满 0.20~0.85
    assert pcts == sorted(pcts)
    assert pcts[-1] == 85


def test_ocr_keeps_table_band_when_table_present():
    out = _feed_all([
        "Pipeline processing-window multi-file run. doc_count=1, total_pages=1, "
        "window_size=64, total_batches=1",
        "Layout Predict: 100%|##| 1/1 [00:00<?, ?it/s]",
        "Table orientation: 100%|##| 1/1 [00:00<?, ?it/s]",
        "OCR-det ch: 0%|##| 0/3 [00:00<?, ?it/s]",
    ])
    assert [p["pct"] for p in out] == [25, 34, 34]


def test_unknown_lines_are_ignored():
    tk = _ProgressTracker()
    assert tk.feed("Completed batch 1/2 | Processed 64/128 pages | 1 of 2 batches finished") is None
    assert tk.feed("Seal Predict:  50%|##| 1/2 [00:00<?, ?it/s]") is None
    assert tk.feed("Submitting batch 1/2 | 1 document, 128 pages in this batch") is None


def test_multi_batch_interpolates_and_stays_monotonic():
    lines = [
        "Pipeline processing-window multi-file run. doc_count=1, total_pages=128, "
        "window_size=64, total_batches=2",
        "Pipeline processing window batch 1/2: 64/128 pages, batch_pages=64",
        "Layout Predict: 100%|##| 64/64 [00:00<?, ?it/s]",
        "OCR-rec Predict: 100%|##| 20/20 [00:00<?, ?it/s]",
        "Processing pages: 100%|##| 64/128 [00:00<?, ?it/s]",
        "Pipeline processing window batch 2/2: 128/128 pages, batch_pages=64",
        "Layout Predict: 100%|##| 64/64 [00:00<?, ?it/s]",
        "OCR-rec Predict: 100%|##| 20/20 [00:00<?, ?it/s]",
        "Processing pages: 100%|##| 128/128 [00:00<?, ?it/s]",
    ]
    out = _feed_all(lines)
    pcts = [p["pct"] for p in out]
    assert pcts == sorted(pcts)
    assert pcts[-1] == 85
