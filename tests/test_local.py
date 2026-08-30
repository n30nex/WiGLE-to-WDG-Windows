from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import sys
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "wigle_to_wdg.py"
spec = importlib.util.spec_from_file_location("wigle_to_wdg", MODULE_PATH)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


SAMPLE_TEMPLATE = """WigleWifi-1.6,appRelease=test
MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type
AA:BB:CC:DD:EE:01,Recent,[WPA2],{recent},6,2437,-50,1.25,2.5,300,5,,,WIFI
AA:BB:CC:DD:EE:02,Old,[WPA2],{old},11,2462,-60,1.5,2.75,300,5,,,WIFI
AA:BB:CC:DD:EE:03,Bluetooth,,{recent},0,0,-70,1.5,2.75,300,5,,,BT
AA:BB:CC:DD:EE:04,No GPS,,{recent},1,2412,-75,0,0,0,0,,,WIFI
"""


class TestFiltering(unittest.TestCase):
    def test_only_recent_wifi_with_gps_survives(self):
        now = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
        text = SAMPLE_TEMPLATE.format(
            recent=(now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            old=(now - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        rows, stats = app.filter_wigle_csv(
            text,
            cutoff=now - timedelta(hours=24),
            end=now,
            already_sent=set(),
            run_seen=set(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].values["SSID"], "Recent")
        self.assertEqual(stats.too_old_rows, 1)
        self.assertEqual(stats.non_wifi_rows, 1)
        self.assertEqual(stats.no_gps_rows, 1)

    def test_state_fingerprint_is_skipped(self):
        now = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        text = SAMPLE_TEMPLATE.format(recent=recent, old=recent)
        first_rows, _ = app.filter_wigle_csv(
            text,
            cutoff=now - timedelta(hours=24),
            end=now,
            already_sent=set(),
            run_seen=set(),
        )
        self.assertTrue(first_rows)
        sent = {first_rows[0].fingerprint}
        second_rows, stats = app.filter_wigle_csv(
            text,
            cutoff=now - timedelta(hours=24),
            end=now,
            already_sent=sent,
            run_seen=set(),
        )
        self.assertNotIn("Recent", [row.values["SSID"] for row in second_rows])
        self.assertEqual(stats.already_sent_rows, 1)


    def test_future_row_is_rejected(self):
        now = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
        future = (now + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        text = SAMPLE_TEMPLATE.format(recent=future, old=future)
        rows, stats = app.filter_wigle_csv(
            text,
            cutoff=now - timedelta(hours=24),
            end=now,
            already_sent=set(),
            run_seen=set(),
        )
        self.assertEqual(rows, [])
        self.assertEqual(stats.future_rows, 3)

    def test_writes_valid_two_line_wigle_header(self):
        now = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
        text = SAMPLE_TEMPLATE.format(
            recent=(now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            old=(now - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        rows, _ = app.filter_wigle_csv(
            text,
            cutoff=now - timedelta(hours=24),
            end=now,
            already_sent=set(),
            run_seen=set(),
        )
        with tempfile.TemporaryDirectory() as td:
            csv_path, gzip_path = app.write_batch_files(rows, output_dir=Path(td), end=now)
            lines = csv_path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines[0].startswith("WigleWifi-1.6,"))
            self.assertEqual(lines[1], ",".join(app.WIGLE_16_COLUMNS))
            self.assertTrue(gzip_path.exists())


class TestCredentials(unittest.TestCase):
    def test_combined_base64_token(self):
        raw = "API-NAME:API-TOKEN"
        import base64

        encoded = base64.b64encode(raw.encode()).decode()
        self.assertEqual(app._combined_wigle_authorization(encoded), "Basic " + encoded)

    def test_raw_name_and_token(self):
        auth = app._combined_wigle_authorization("API-NAME:API-TOKEN")
        self.assertTrue(auth.startswith("Basic "))


class TestLatestOnlySafety(unittest.TestCase):
    def test_queries_exactly_one_newest_transaction(self):
        payload = {
            "success": True,
            "results": [{"transid": "newest"}, {"transid": "older"}],
        }
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            app, "request_json", return_value=payload
        ) as request:
            result = app.fetch_latest_wigle_transaction(
                "Basic test", logger=app.Logger(Path(td) / "test.log")
            )

        self.assertEqual(result["transid"], "newest")
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.args[0].endswith("pagestart=0&pageend=1"))

    def test_same_successful_transaction_is_not_downloaded_again(self):
        now = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "state.json"
            state_path.write_text(
                json.dumps({
                    "version": 1,
                    "sent": {},
                    "last_successful_transaction_id": "newest",
                }),
                encoding="utf-8",
            )
            with mock.patch.object(
                app,
                "fetch_latest_wigle_transaction",
                return_value={"transid": "newest", "status": "D"},
            ), mock.patch.object(app, "download_wigle_csv") as download:
                batch, _ = app.build_batch(
                    app.Credentials("Basic test", "a" * 64),
                    cutoff=now - timedelta(hours=24),
                    end=now,
                    output_dir=root / "out",
                    state_path=state_path,
                    logger=app.Logger(root / "test.log"),
                )

        self.assertIsNone(batch)
        download.assert_not_called()


class TestWdgPolling(unittest.TestCase):
    def test_async_upload_polls_until_done(self):
        with tempfile.TemporaryDirectory() as td:
            payload = Path(td) / "sample.csv.gz"
            payload.write_bytes(b"not-real-gzip-but-stream-is-mocked")
            submit = {"ok": True, "job_id": 42, "poll_url": "/api/v2/upload-job/42"}
            jobs = iter([
                {"ok": True, "job_id": 42, "status": "processing"},
                {"ok": True, "job_id": 42, "status": "done", "result": {"imported": 3}},
            ])
            with mock.patch.object(
                app, "_stream_multipart_upload",
                return_value=(202, {}, json.dumps(submit).encode()),
            ), mock.patch.object(
                app, "request_json", side_effect=lambda *args, **kwargs: next(jobs)
            ), mock.patch.object(app.time, "sleep"):
                logger = app.Logger(Path(td) / "test.log")
                result = app.upload_wdg_v2("test-key", payload, logger=logger)
            self.assertTrue(result.ok)
            self.assertEqual(result.job_id, 42)
            self.assertEqual(result.result["imported"], 3)


if __name__ == "__main__":
    unittest.main()
