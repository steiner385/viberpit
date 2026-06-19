import unittest
from datetime import timezone


class TestDetect(unittest.TestCase):
    def test_result_success_returns_none(self):
        from ccswitch.detect import detect_limit_in_result
        self.assertIsNone(detect_limit_in_result({"type": "result", "is_error": False, "result": "hi"}))

    def test_result_usage_limit_detected_with_reset(self):
        from ccswitch.detect import detect_limit_in_result
        r = detect_limit_in_result({
            "type": "result", "is_error": True,
            "error": {"message": "Claude usage limit reached", "resets_at": "2026-06-19T18:00:00+00:00"},
        })
        self.assertIsNotNone(r)
        self.assertEqual(r.resets_at.tzinfo, timezone.utc)

    def test_result_429_without_reset(self):
        from ccswitch.detect import detect_limit_in_result
        r = detect_limit_in_result({"type": "result", "is_error": True, "error": {"status": 429}})
        self.assertIsNotNone(r)
        self.assertIsNone(r.resets_at)

    def test_jsonl_rate_limit_line(self):
        from ccswitch.detect import detect_limit_in_jsonl_line
        r = detect_limit_in_jsonl_line({"type": "system", "subtype": "rate_limit", "resets_at": "2026-06-19T18:00:00+00:00"})
        self.assertIsNotNone(r)

    def test_jsonl_normal_line_none(self):
        from ccswitch.detect import detect_limit_in_jsonl_line
        self.assertIsNone(detect_limit_in_jsonl_line({"type": "assistant", "message": {"content": "ok"}}))
