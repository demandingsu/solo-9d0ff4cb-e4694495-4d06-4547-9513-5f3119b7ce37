"""HTTP 端到端自测：真实 gunicorn（2 worker × 4 线程）下的存法互换与并发补录。

前置：环境中已安装 Flask/gunicorn。
运行： GUNICORN_BIN=/tmp/venv/bin/gunicorn python -m unittest tests.test_http -v
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----labeltest" + uuid.uuid4().hex
    lines = []
    for name, value in fields.items():
        lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    lines.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"csv_file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: text/csv\r\n\r\n".encode()
    )
    lines.append(content)
    lines.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(lines), boundary


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError("server did not start in time")


def _wait_health(base: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=3) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.3)
    raise RuntimeError(f"health check failed: {last}")


class HttpEndToEndTest(unittest.TestCase):
    proc: subprocess.Popen | None = None
    workdir: str | None = None
    base = ""

    @classmethod
    def setUpClass(cls) -> None:
        gunicorn = os.environ.get("GUNICORN_BIN") or shutil.which("gunicorn")
        if not gunicorn or not os.path.exists(gunicorn):
            raise unittest.SkipTest("未找到 gunicorn，跳过 HTTP 端到端测试（设 GUNICORN_BIN 指定）")
        cls.workdir = tempfile.mkdtemp(prefix="label-http-")
        db_path = os.path.join(cls.workdir, "labels.db")
        cls.port = int(os.environ.get("TEST_PORT", "18000"))
        env = dict(os.environ, LABEL_DB_PATH=db_path, LABEL_BUSY_TIMEOUT_MS="15000")
        cls.proc = subprocess.Popen(
            [gunicorn, "--bind", f"127.0.0.1:{cls.port}",
             "--workers", "2", "--threads", "4", "--timeout", "30",
             "app.wsgi:app"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        try:
            _wait_port(cls.port)
            _wait_health(cls.base)
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.proc is not None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
            output = cls.proc.stdout.read() if cls.proc.stdout else ""
            if output:
                sys.stderr.write("\n--- gunicorn log ---\n" + output + "\n")
        if cls.workdir and os.path.isdir(cls.workdir):
            shutil.rmtree(cls.workdir, ignore_errors=True)

    def _upload(self, exhibit: str, rows: list[tuple[str, str]], headers: bool = False) -> dict:
        lines = []
        if headers:
            lines.append("plate_no,language")
        lines.extend(f"{p},{lang}" for p, lang in rows)
        body, boundary = _multipart({"exhibit_no": exhibit}, "batch.csv",
                                    ("\n".join(lines) + "\n").encode("utf-8"))
        req = urllib.request.Request(
            f"{self.base}/api/exhibits/{exhibit}/batches", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read())
                self.assertEqual(resp.status, 200)
                return payload
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read())

    def _get(self, exhibit: str) -> dict:
        with urllib.request.urlopen(f"{self.base}/api/exhibits/{exhibit}", timeout=10) as resp:
            return json.loads(resp.read())

    # ---------- 巡展分站 ----------

    def _multipart_raw(self, fields: dict[str, str], filename: str, content: bytes):
        body, boundary = _multipart(fields, filename, content)
        return body, boundary

    def _station_create(self, station: str, parent: str) -> tuple[int, dict]:
        body, boundary = _multipart(
            {"station_no": station, "parent_exhibit_no": parent}, "station.csv", b""
        )
        req = urllib.request.Request(
            f"{self.base}/api/stations", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.status, json.loads(exc.read())

    def _station_diff(
        self, station: str, lines: list[str], raw: bytes | None = None
    ) -> tuple[int, dict]:
        content = raw if raw is not None else ("\n".join(lines) + "\n").encode("utf-8")
        body, boundary = self._multipart_raw({}, "diffs.csv", content)
        req = urllib.request.Request(
            f"{self.base}/api/stations/{station}/diffs", data=body, method="PUT",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.status, json.loads(exc.read())

    def _station_get(self, station: str) -> dict:
        with urllib.request.urlopen(f"{self.base}/api/stations/{station}", timeout=10) as resp:
            return json.loads(resp.read())

    def test_01_dedup_and_sparse_to_dense_switch_over_http(self):
        exhibit = "HTTP-E1"
        r1 = self._upload(exhibit, [("P1", "zh"), ("P2", "zh"), ("P2", "en"), ("P2", "en")])
        self.assertEqual(r1["status"], "committed")
        self.assertEqual(r1["storage_mode"], "sparse")
        # 4 行原始数据（含一个重复行），文件内去重后 3 种搭配，全部为新增
        self.assertEqual((r1["rows_in_file"], r1["unique_rows"]), (4, 3))
        self.assertEqual(r1["new_pairs"], 3)
        d1 = self._get(exhibit)
        self.assertEqual(d1["missing_pairs"], [["P1", "en"]])

        r2 = self._upload(exhibit, [("P1", "en")])
        self.assertEqual(r2["storage_mode"], "dense")
        self.assertEqual(r2["switched_from"], "sparse")
        d2 = self._get(exhibit)
        self.assertTrue(d2["is_complete"])
        self.assertEqual(d2["pair_count"], 4)

        # 重复上传不产生新配对
        r3 = self._upload(exhibit, [("P1", "zh"), ("P1", "en"), ("P2", "zh"), ("P2", "en")])
        self.assertEqual(r3["new_pairs"], 0)
        self.assertEqual(r3["storage_mode"], "dense")

    def test_02_dense_to_sparse_switch_over_http(self):
        exhibit = "HTTP-E2"
        self._upload(exhibit, [("P1", "zh"), ("P1", "en"), ("P2", "zh"), ("P2", "en")])
        r = self._upload(exhibit, [("P3", "zh")])  # 引入缺项
        self.assertEqual(r["storage_mode"], "sparse")
        self.assertEqual(r["switched_from"], "dense")
        d = self._get(exhibit)
        self.assertEqual(d["pair_count"], 5)
        self.assertEqual({tuple(x) for x in d["missing_pairs"]}, {("P3", "en")})

    def test_03_bad_csv_rejected_without_touching_data(self):
        exhibit = "HTTP-E3"
        self._upload(exhibit, [("P1", "zh"), ("P1", "en")])
        body, boundary = _multipart({"exhibit_no": exhibit}, "bad.csv",
                                    b"plate_no,language\nP9,\n")
        req = urllib.request.Request(
            f"{self.base}/api/exhibits/{exhibit}/batches", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("应返回 422")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.status, 422)
        d = self._get(exhibit)
        self.assertEqual(d["pair_count"], 2)  # 拒收后已提交资料不变
        self.assertEqual(d["batches"][0]["status"], "rejected")

    def test_04_concurrent_same_exhibit_all_batches_land(self):
        exhibit = "HTTP-CONC"
        plates = [f"P{i}" for i in range(8)]

        def one(plate: str) -> dict:
            return self._upload(exhibit, [(plate, "zh"), (plate, "en")])

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(one, plates))

        self.assertTrue(all(r["status"] == "committed" for r in results), results)
        d = self._get(exhibit)
        self.assertEqual(d["pair_count"], 16)
        self.assertEqual(d["storage_mode"], "dense")
        self.assertEqual(len(d["plates"]), 8)
        self.assertEqual(len([b for b in d["batches"] if b["status"] == "committed"]), 8)

    def test_05_concurrent_interleaved_exhibits(self):
        def one(exhibit: str, plate: str) -> dict:
            return self._upload(exhibit, [(plate, "zh"), (plate, "en")])

        tasks = []
        for i in range(12):
            exhibit = "HTTP-A" if i % 2 == 0 else "HTTP-B"
            tasks.append((exhibit, f"{exhibit}-P{i // 2}"))
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda t: one(*t), tasks))
        self.assertTrue(all(r["status"] == "committed" for r in results))

        da = self._get("HTTP-A")
        db = self._get("HTTP-B")
        self.assertEqual(da["pair_count"], 12)  # 6 图版 × 2 语种
        self.assertEqual(db["pair_count"], 12)
        self.assertEqual(da["storage_mode"], "dense")
        self.assertEqual(db["storage_mode"], "dense")

    def test_06_station_early_exclude_then_parent_backfill_over_http(self):
        """题目要求的网页试跑主链路：提前排除 -> 主展补录 -> 排除仍生效。"""
        parent = "HTTP-TOUR-MAIN"
        station = "HTTP-TOUR-S1"
        # 主展初始只有 (P1, zh)，sparse
        self._upload(parent, [("P1", "zh")])
        status, _ = self._station_create(station, parent)
        self.assertEqual(status, 201)
        # 选用未登记主展应被拒
        status, payload = self._station_create("HTTP-TOUR-BAD", "GHOST-MAIN")
        self.assertEqual(status, 404)

        # 提前排除主展尚不存在的 (P1, en)，并增补当地语种 (P1, ja)
        status, r = self._station_diff(
            station,
            ["plate_no,language,action", "P1,en,exclude", "P1,ja,add"],
        )
        self.assertEqual(status, 200)
        self.assertEqual((r["add_count"], r["exclude_count"]), (1, 1))
        d1 = self._station_get(station)
        self.assertEqual(
            {x["language"]: x["source"] for x in d1["by_plate"]["P1"]},
            {"zh": "parent", "ja": "station_add"},
        )
        self.assertEqual(d1["pair_count"], 2)

        # 同文件相反操作整单拒收（422），旧差异继续生效
        status, payload = self._station_diff(
            station, ["P9,fr,add", "P9,fr,exclude"]
        )
        self.assertEqual(status, 422)
        self.assertIn("整单拒收", payload["error"])
        d2 = self._station_get(station)
        self.assertEqual(d2["excludes"], [["P1", "en"]])  # 原差异未动

        # 主展补录 (P1,en) 及 P2 全套，主展升 dense
        self._upload(parent, [("P1", "en"), ("P2", "zh"), ("P2", "en")])
        d3 = self._station_get(station)
        self.assertEqual(d3["parent_storage_mode"], "dense")
        langs_p1 = {x["language"]: x["source"] for x in d3["by_plate"]["P1"]}
        self.assertNotIn("en", langs_p1)          # 提前排除仍生效
        self.assertEqual(langs_p1["ja"], "station_add")
        # 主展 4 搭配 − 排除 1 + 主展外增补 1 = 4
        self.assertEqual(d3["pair_count"], 4)

        # 空清单整份替换：清空差异，恢复直接沿用主展
        status, r = self._station_diff(station, [], raw=b"plate_no,language,action\n")
        self.assertEqual(status, 200)
        self.assertEqual(r["unique_rows"], 0)
        d4 = self._station_get(station)
        self.assertEqual(d4["adds"], [])
        self.assertEqual(d4["excludes"], [])
        self.assertEqual(d4["pair_count"], 4)

    def test_07_station_replace_and_dense_count_over_http(self):
        parent = "HTTP-TOUR-DENSE"
        station = "HTTP-TOUR-S2"
        self._upload(parent, [("P1", "zh"), ("P1", "en"), ("P2", "zh"), ("P2", "en")])
        self._station_create(station, parent)
        # 第一份差异
        self._station_diff(station, ["P1,fr,add", "P2,en,exclude"])
        d1 = self._station_get(station)
        self.assertEqual(d1["pair_count"], 4)  # 4 − 1 + 1
        # 第二份整份替换
        self._station_diff(station, ["P2,zh,exclude"])
        d2 = self._station_get(station)
        self.assertEqual(d2["adds"], [])
        self.assertEqual(d2["excludes"], [["P2", "zh"]])
        self.assertEqual(d2["pair_count"], 3)
        # 各站只存自己的差异
        other = self._station_get("HTTP-TOUR-S1")
        self.assertEqual(other["excludes"], [])

    def test_08_pages_render(self):
        for path in (
            "/",
            "/exhibits/HTTP-E1",
            "/stations",
            "/stations/HTTP-TOUR-S1",
            "/healthz",
        ):
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
        try:
            urllib.request.urlopen(self.base + "/stations/NO-SUCH", timeout=10)
            self.fail("应 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
