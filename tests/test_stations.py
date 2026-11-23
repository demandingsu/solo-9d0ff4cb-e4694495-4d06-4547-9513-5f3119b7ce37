"""分站存储层自测：差异整份替换、来源标注、提前排除、主展补录/换存法自动跟上。

仅依赖标准库： python -m unittest tests.test_stations -v
"""

from __future__ import annotations

import os
import tempfile
import unittest

from app import csvio, stations, storage
from app.db import connect, init_db


class StationTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        os.environ["LABEL_DB_PATH"] = self.db_path
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def conn(self):
        return connect(self.db_path)

    def register(self, conn, station="S1", parent="E1"):
        stations.register_station(conn, station, parent)

    def langs(self, detail, plate):
        return {item["language"]: item["source"] for item in detail["by_plate"][plate]}

    def parse(self, text: str):
        return csvio.parse_diffs(text.encode("utf-8"))


class TestStationRegistration(StationTestBase):
    def test_parent_must_be_registered(self):
        with self.conn() as conn:
            with self.assertRaises(LookupError):
                self.register(conn, "S1", "GHOST")

    def test_re_register_is_idempotent_and_keeps_diffs(self):
        with self.conn() as conn:
            storage.merge_and_store(conn, "E1", [("P1", "zh")])
            self.register(conn, "S1", "E1")
            stations.replace_diffs(conn, "S1", [("P1", "fr", "add")])
            # 再次登记同站（同一主展）不报错，差异保留
            stations.register_station(conn, "S1", "E1")
            detail = stations.get_station(conn, "S1")
            self.assertEqual(detail["adds"], [["P1", "fr"]])


class TestDiffReplacement(StationTestBase):
    def test_whole_file_replaces_previous_diffs(self):
        with self.conn() as conn:
            storage.merge_and_store(
                conn, "E1", [("P1", "zh"), ("P1", "en"), ("P2", "zh"), ("P2", "en")]
            )
            self.register(conn)
            stations.replace_diffs(conn, "S1", [("P1", "fr", "add"), ("P2", "en", "exclude")])
            detail1 = stations.get_station(conn, "S1")
            self.assertEqual(len(detail1["adds"]), 1)
            self.assertEqual(len(detail1["excludes"]), 1)

            # 第二份文件整份替换：只剩一条排除，旧增补消失
            stations.replace_diffs(conn, "S1", [("P1", "zh", "exclude")])
            detail2 = stations.get_station(conn, "S1")
            self.assertEqual(detail2["adds"], [])
            self.assertEqual(detail2["excludes"], [["P1", "zh"]])

    def test_empty_list_clears_diffs_and_restores_parent(self):
        with self.conn() as conn:
            storage.merge_and_store(
                conn, "E1", [("P1", "zh"), ("P1", "en"), ("P2", "zh"), ("P2", "en")]
            )
            self.register(conn)
            stations.replace_diffs(conn, "S1", [("P1", "fr", "add"), ("P2", "en", "exclude")])
            # 空清单（空 CSV 字节流）= 清空差异
            parsed = self.parse("")
            self.assertEqual(parsed.diffs, [])
            result = stations.replace_diffs(conn, "S1", parsed)
            self.assertEqual((result.unique_rows, result.add_count, result.exclude_count), (0, 0, 0))
            detail = stations.get_station(conn, "S1")
            self.assertEqual(detail["adds"], [])
            self.assertEqual(detail["excludes"], [])
            self.assertEqual(detail["pair_count"], 4)  # 恢复直接沿用主展

    def test_conflicting_actions_in_same_file_rejected_before_touching_data(self):
        with self.conn() as conn:
            storage.merge_and_store(conn, "E1", [("P1", "zh"), ("P1", "en")])
            self.register(conn)
            stations.replace_diffs(conn, "S1", [("P1", "fr", "add")])

            with self.assertRaises(csvio.CsvFormatError):
                self.parse("plate_no,language,action\nP9,ja,add\nP9,ja,exclude\n")
            # 带表头、同搭配相反操作；解析层拒收
            with self.assertRaises(csvio.CsvFormatError):
                self.parse("图版号,语种,操作\nP9,ja,增补\nP9,ja,排除\n")

            # 模拟 Web 层：拒收只留痕，旧差异继续生效
            try:
                self.parse("P9,ja,add\nP9,ja,exclude\n")
            except csvio.CsvFormatError as exc:
                stations.log_rejected_diff(conn, "S1", str(exc))
            detail = stations.get_station(conn, "S1")
            self.assertEqual(detail["adds"], [["P1", "fr"]])  # 原差异未动
            self.assertEqual(detail["diff_logs"][0]["status"], "rejected")

    def test_duplicate_same_action_folds_to_one(self):
        parsed = self.parse("P1,fr,add\nP1,fr,add\nP1,ja,exclude\n")
        self.assertEqual(parsed.raw_data_rows, 3)
        self.assertEqual(len(parsed.diffs), 2)


class TestEffectiveView(StationTestBase):
    def test_sources_marked_and_duplicate_counts_once(self):
        with self.conn() as conn:
            # dense 主展：P1/P2 × zh/en
            storage.merge_and_store(
                conn, "E1", [("P1", "zh"), ("P1", "en"), ("P2", "zh"), ("P2", "en")]
            )
            self.register(conn)
            # 增补一个主展已有的搭配（P1,en）和一个新语种（P1,fr）
            stations.replace_diffs(
                conn, "S1", [("P1", "en", "add"), ("P1", "fr", "add"), ("P2", "en", "exclude")]
            )
            detail = stations.get_station(conn, "S1")
            p1 = self.langs(detail, "P1")
            self.assertEqual(p1["en"], "station_add")   # 两处都有 -> 分站增补
            self.assertEqual(p1["zh"], "parent")
            self.assertEqual(p1["fr"], "station_add")
            self.assertNotIn("en", self.langs(detail, "P2"))  # 排除生效
            # 总数：主展 4 − 排除 1（P2,en 在主展内）+ 主展外增补 1（fr）= 4
            self.assertEqual(detail["pair_count"], 4)

    def test_early_exclusion_still_holds_after_parent_backfill(self):
        """题目核心场景：提前排除的搭配后来补到主展，本站仍不印。"""
        with self.conn() as conn:
            # 主展初始 sparse：P1 只有 zh
            storage.merge_and_store(conn, "E1", [("P1", "zh")])
            self.register(conn)
            # 提前排除一个主展里尚不存在的搭配
            stations.replace_diffs(conn, "S1", [("P1", "en", "exclude")])
            before = stations.get_station(conn, "S1")
            self.assertEqual(set(self.langs(before, "P1")), {"zh"})

            # 主展后来补录 (P1, en)，并补齐为 dense
            storage.merge_and_store(conn, "E1", [("P1", "en"), ("P2", "zh"), ("P2", "en")])
            self.assertEqual(storage.get_exhibit(conn, "E1")["storage_mode"], "dense")

            after = stations.get_station(conn, "S1")
            self.assertNotIn("en", self.langs(after, "P1"))  # 仍不印
            self.assertIn("zh", self.langs(after, "P1"))
            # 主展 4 个搭配，被排除 1 个，无主展外增补 -> 3
            self.assertEqual(after["pair_count"], 3)
            self.assertEqual(after["parent_storage_mode"], "dense")

    def test_station_follows_parent_storage_switch_both_ways(self):
        with self.conn() as conn:
            # dense 主展
            storage.merge_and_store(
                conn, "E1", [("P1", "zh"), ("P1", "en"), ("P2", "zh"), ("P2", "en")]
            )
            self.register(conn)
            stations.replace_diffs(conn, "S1", [("P3", "de", "add")])
            d_dense = stations.get_station(conn, "S1")
            self.assertEqual(d_dense["pair_count"], 5)  # 4 + 1 主展外增补

            # 主展引入缺项切 sparse，分站自动跟上且增补保留
            storage.merge_and_store(conn, "E1", [("P3", "zh")])
            d_sparse = stations.get_station(conn, "S1")
            self.assertEqual(d_sparse["parent_storage_mode"], "sparse")
            self.assertEqual(d_sparse["pair_count"], 6)  # 主展 5 + de 增补
            self.assertEqual(self.langs(d_sparse, "P3")["de"], "station_add")

            # 补回 dense
            storage.merge_and_store(conn, "E1", [("P3", "en")])
            d_again = stations.get_station(conn, "S1")
            self.assertEqual(d_again["parent_storage_mode"], "dense")
            self.assertEqual(d_again["pair_count"], 7)  # 主展 6 + de 增补

    def test_dense_parent_count_never_materializes_cartesian_product(self):
        """dense 主展大清单：总数用基数算术，且与明细口径一致。"""
        with self.conn() as conn:
            plates = [f"P{i:04d}" for i in range(200)]
            langs = ["zh", "en", "fr", "ja", "de"]
            pairs = [(p, lang) for p in plates for lang in langs]
            storage.merge_and_store(conn, "BIG", pairs)
            self.assertEqual(storage.get_exhibit(conn, "BIG")["storage_mode"], "dense")
            stations.register_station(conn, "BIG-S", "BIG")
            stations.replace_diffs(
                conn,
                "BIG-S",
                [("P0000", "zh", "exclude"), ("P0000", "ko", "add"),
                 ("PX999", "es", "add")],
            )
            detail = stations.get_station(conn, "BIG-S")
            # 1000 − 1 个落在主展内的排除 + 2 个主展外增补 = 1001
            self.assertEqual(detail["pair_count"], 1001)
            self.assertEqual(len(detail["by_plate"]["P0000"]), 5)  # 4 主展 + ko
            self.assertEqual(self.langs(detail, "P0000")["ko"], "station_add")

    def test_sparse_parent_excludes_only_affect_existing_pairs(self):
        with self.conn() as conn:
            storage.merge_and_store(conn, "E1", [("P1", "zh"), ("P2", "zh"), ("P2", "en")])
            self.register(conn)
            # 排除一个主展不存在的搭配，不影响总数；增补一个新搭配
            stations.replace_diffs(
                conn, "S1", [("P1", "en", "exclude"), ("P3", "fr", "add")]
            )
            detail = stations.get_station(conn, "S1")
            self.assertEqual(detail["pair_count"], 4)  # 主展 3（排除未命中）+ 增补 1
            self.assertEqual(sorted(detail["plates"]), ["P1", "P2", "P3"])

    def test_stations_store_only_their_own_diffs(self):
        with self.conn() as conn:
            storage.merge_and_store(conn, "E1", [("P1", "zh"), ("P1", "en")])
            stations.register_station(conn, "SA", "E1")
            stations.register_station(conn, "SB", "E1")
            stations.replace_diffs(conn, "SA", [("P1", "fr", "add")])
            stations.replace_diffs(conn, "SB", [("P1", "zh", "exclude")])
            da = stations.get_station(conn, "SA")
            db = stations.get_station(conn, "SB")
            self.assertEqual(da["adds"], [["P1", "fr"]])
            self.assertEqual(da["excludes"], [])
            self.assertEqual(db["adds"], [])
            self.assertEqual(db["excludes"], [["P1", "zh"]])
            self.assertEqual(db["pair_count"], 1)  # 只剩 en

    def test_parent_missing_keeps_station_diffs_and_recovers(self):
        with self.conn() as conn:
            storage.merge_and_store(conn, "E1", [("P1", "zh")])
            self.register(conn)
            stations.replace_diffs(conn, "S1", [("P1", "fr", "add")])
        # 模拟主展被清空（实际业务不会删除，仅验证容错）：直接删掉主展状态行
        with self.conn() as conn:
            conn.execute("DELETE FROM storage_state WHERE exhibit_no='E1'")
            conn.execute("DELETE FROM dense_plates WHERE exhibit_no='E1'")
            detail = stations.get_station(conn, "S1")
            self.assertFalse(detail["parent_registered"])
            self.assertEqual(detail["pair_count"], 1)  # 增补仍可展示
        with self.conn() as conn:
            storage.merge_and_store(conn, "E1", [("P1", "zh"), ("P1", "en")])
            detail = stations.get_station(conn, "S1")
            self.assertTrue(detail["parent_registered"])
            self.assertEqual(detail["pair_count"], 3)  # zh, en + fr


class TestDiffCsvParsing(StationTestBase):
    def test_header_detection_and_action_aliases(self):
        parsed = self.parse(
            "plate_no,language,action\nP1,中文,add\nP2,English,exclude\n"
        )
        self.assertEqual(parsed.diffs, [("P1", "中文", "add"), ("P2", "English", "exclude")])
        parsed2 = self.parse("图版号,语种,操作\nP1,法语,加印\nP2,英语,取消\n")
        self.assertEqual(parsed2.diffs, [("P1", "法语", "add"), ("P2", "英语", "exclude")])

    def test_header_only_is_empty_list(self):
        parsed = self.parse("plate_no,language,action\n")
        self.assertEqual(parsed.diffs, [])
        self.assertEqual(parsed.raw_data_rows, 0)

    def test_bad_action_rejected(self):
        with self.assertRaises(csvio.CsvFormatError):
            self.parse("P1,fr,print\n")

    def test_gb18030_encoding_supported(self):
        raw = "图版号,语种,操作\nP1,中文,增补\n".encode("gb18030")
        parsed = csvio.parse_diffs(raw)
        self.assertEqual(parsed.diffs, [("P1", "中文", "add")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
