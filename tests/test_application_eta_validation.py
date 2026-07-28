from scripts.validate_application_eta import run_validation


def test_frozen_heldout_application_eta_traces_complete_and_report_metrics():
    results = run_validation()
    heldout = [item for item in results if item.split == "heldout"]
    assert len(heldout) >= 4
    for item in heldout:
        assert item.completed + item.failed >= 8, item
        assert item.eta_samples >= 2, item
        assert item.time_to_first_eta_seconds is not None, item
        assert item.median_absolute_percentage_error is not None, item
        assert item.interval_coverage is not None, item
        assert item.longest_frozen_display_seconds is not None, item
        assert item.final_drain_absolute_error_seconds is not None, item
