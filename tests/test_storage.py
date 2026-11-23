"""存储层自测：去重追加、存法选择与互换、交错补录、并发不丢更新、失败回滚。

仅依赖标准库，可直接： python -m unittest tests.test_storage -v
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest

from app import storage
from app.db import connect, init_db, run_with_immediate


class StorageTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        os.environ["LABEL_DB_PATH"] = self.db_path
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def conn(self):
        return connect(self.db_path)

    def table_counts(self, conn, exhibit: str) -> dict[str, int]:
        return {
            "state": conn.execute(
                "SELECT COUNT(*) FROM storage_state WHERE exhibit_no=?", (exhibit,)
            ).fetchone()[0],
            "dense_plates": conn.execute(
                "SELECT COUNT(*) FROM dense_plates WHERE exhibit_no=?", (exhibit,)
            ).fetchone()[0],
            "dense_langs": conn.execute(
                "SELECT COUNT(*) FROM dense_languages WHERE exhibit_no=?", (exhibit,)
            ).fetchone()[0],
            "sparse": conn.execute(
                "SELECT COUNT(*) FROM sparse_pairs WHERE exhibit_no=?", (exhibit,)
            ).fetchone()[0],
        }


class TestStorageModeSelection(StorageTestBase):
    def test_complete_cartesian_uses_dense_lists(self):
        with self.conn() as conn:
            r = storage.merge_and_store(
                conn, "E1", [("P1", "zh"), ("P1", "en"), ("P2", "zh"), ("P2", "en")]
            )
            self.assertEqual(r.storage_mode, "dense")
            counts = self.table_counts(conn, "E1")
            self.assertEqual((counts["dense_plates"], counts["dense_langs"], counts["sparse"]), (2, 2, 0))
            detail = storage.get_exhibit(conn, "E1")
            self.assertTrue(detail["is_complete"])
            self.assertEqual(detail["pair_count"], 4)
            self.assertEqual(detail["by_plate"]["P1"], ["en", "zh"])

    def test_missing_pairs_uses_sparse_pairs(self):
        with self.conn() as conn:
            r = storage.merge_and_store(
                conn, "E1", [("P1", "zh"), ("P2", "zh"), ("P2", "en")]
            )
            self.assertEqual(r.storage_mode, "sparse")
            counts = self.table_counts(conn, "E1")
            self.assertEqual((counts["dense_plates"], counts["dense_langs"], counts["sparse"]), (0, 0, 3))
            detail = storage.get_exhibit(conn, "E1")
            self.assertFalse(detail["is_complete"])
            self.assertEqual(detail["missing_pairs"], [["P1", "en"]])
            self.assertEqual(detail["by_plate"]["P1"], ["zh"])


class TestStorageModeSwitching(StorageTestBase):
    def test_sparse_then_completing_batch_switches_to_dense_without_loss(self):
        with self.conn() as conn:
            storage.merge_and_store(conn, "E1", [("P1", "zh"), ("P2", "zh"), ("P2", "en")])
            detail_before = storage.get_exhibit(conn, "E1")
            self.assertEqual(detail_before["storage_mode"], "sparse")

            r = storage.merge_and_store(conn, "E1", [("P1", "en")])  # 补齐缺项
            self.assertEqual(r.storage_mode, "dense")
            self.assertEqual(r.switched_from, "sparse")

            detail = storage.get_exhibit(conn, "E1")
            self.assertEqual(detail["pair_count"], 4)
            self.assertEqual(set(detail["plates"]), {"P1", "P2"})
            self.assertEqual(set(detail["languages"]), {"en", "zh"})
            self.assertTrue(detail["is_complete"])
            counts = self.table_counts(conn, "E1")
            self.assertEqual(counts["sparse"], 0)  # 旧存法数据已清空

    def test_dense_then_partial_batch_switches_to_sparse_without_loss(self):
        with self.conn() as conn:
            storage.merge_and_store(
                conn, "E1", [("P1", "zh"), ("P1", "en"), ("P2", "zh"), ("P2", "en")]
            )
            # 新图版 P3 只有一种语种 -> 合并后出现缺项
            r = storage.merge_and_store(conn, "E1", [("P3", "zh")])
            self.assertEqual(r.storage_mode, "sparse")
            self.assertEqual(r.switched_from, "dense")

            detail = storage.get_exhibit(conn, "E1")
            self.assertEqual(detail["pair_count"], 5)
            self.assertEqual(detail["by_plate"]["P1"], ["en", "zh"])  # 旧资料完整保留
            self.assertEqual(detail["by_plate"]["P3"], ["zh"])
            missing = {tuple(x) for x in detail["missing_pairs"]}
            self.assertEqual(missing, {("P3", "en")})
            counts = self.table_counts(conn, "E1")
            self.assertEqual((counts["dense_plates"], counts["dense_langs"]), (0, 0))

    def test_round_trip_switch_returns_to_dense(self):
        with self.conn() as conn:
            storage.merge_and_store(conn, "E1", [("P1", "zh"), ("P1", "en")])
            self.assertEqual(storage.get_exhibit(conn, "E1")["storage_mode"], "dense")
            storage.merge_and_store(conn, "E1", [("P2", "zh")])
            self.assertEqual(storage.get_exhibit(conn, "E1")["storage_mode"], "sparse")
            storage.merge_and_store(conn, "E1", [("P2", "en")])
            detail = storage.get_exhibit(conn, "E1")
            self.assertEqual(detail["storage_mode"], "dense")
            self.assertEqual(detail["pair_count"], 4)


class TestDedupAndInterleaving(StorageTestBase):
    def test_in_file_and_cross_batch_dedup(self):
        with self.conn() as conn:
            r1 = storage.merge_and_store(
                conn, "E1", [("P1", "zh"), ("P1", "zh"), ("P1", "en")]
            )
            self.assertEqual((r1.rows_in_file, r1.unique_rows, r1.new_pairs, r1.total_pairs), (3, 2, 2, 2))
            r2 = storage.merge_and_store(conn, "E1", [("P1", "zh"), ("P1", "en")])
            self.assertEqual((r2.new_pairs, r2.total_pairs), (0, 2))

    def test_interleaved_batches_across_exhibits_are_independent(self):
        with self.conn() as conn:
            storage.merge_and_store(conn, "E1", [("P1", "zh"), ("P1", "en")])  # dense
            storage.merge_and_store(conn, "E2", [("Q1", "zh")])                 # sparse
            storage.merge_and_store(conn, "E1", [("P2", "zh"), ("P2", "en")])  # 仍 dense
            r = storage.merge_and_store(conn, "E2", [("Q1", "en"), ("Q2", "zh"), ("Q2", "en")])
            self.assertEqual(r.storage_mode, "dense")

            d1 = storage.get_exhibit(conn, "E1")
            d2 = storage.get_exhibit(conn, "E2")
            self.assertEqual((d1["storage_mode"], d1["pair_count"]), ("dense", 4))
            # E2: Q1{zh,en} + Q2{zh,en} = 2 图版 × 2 语种 = 4 种搭配
            self.assertEqual((d2["storage_mode"], d2["pair_count"]), ("dense", 4))
            self.assertEqual(set(d1["plates"]), {"P1", "P2"})
            self.assertEqual(set(d2["plates"]), {"Q1", "Q2"})

    def test_failed_transaction_keeps_committed_data(self):
        with self.conn() as conn:
            storage.merge_and_store(conn, "E1", [("P1", "zh"), ("P1", "en")])

            def boom(tx):
                tx.execute("INSERT INTO sparse_pairs(exhibit_no, plate_no, language) VALUES (?,?,?)",
                           ("E1", "PX", "fr"))
                raise RuntimeError("simulated batch failure")

            with self.assertRaises(RuntimeError):
                run_with_immediate(conn, boom)

            detail = storage.get_exhibit(conn, "E1")
            self.assertEqual(detail["pair_count"], 2)  # 已提交资料原样保留
            self.assertEqual(detail["storage_mode"], "dense")
            logs = detail["batches"]
            self.assertTrue(all(b["status"] == "committed" for b in logs))


class TestConcurrentAppends(StorageTestBase):
    def test_concurrent_same_exhibit_all_batches_land(self):
        """同一展品并发补录：8 个线程各加不同配对，合起来恰为完整笛卡尔积。"""
        errors: list[Exception] = []
        plates = [f"P{i}" for i in range(4)]
        langs = ["zh", "en"]
        # 每个线程负责一个图版的两种语种
        batches = [[(p, lang) for lang in langs] for p in plates]

        def worker(pairs):
            try:
                c = connect(self.db_path)
                try:
                    storage.merge_and_store(c, "E1", pairs)
                finally:
                    c.close()
            except Exception as exc:  # pragma: no cover - 测试断言用
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(b,)) for b in batches]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        with self.conn() as conn:
            detail = storage.get_exhibit(conn, "E1")
            self.assertEqual(detail["pair_count"], 8)
            self.assertTrue(detail["is_complete"])
            self.assertEqual(detail["storage_mode"], "dense")
            committed = conn.execute(
                "SELECT COUNT(*) FROM batch_log WHERE exhibit_no='E1' AND status='committed'"
            ).fetchone()[0]
            self.assertEqual(committed, len(threads))

    def test_concurrent_overlapping_batches_no_duplicates(self):
        errors: list[Exception] = []

        def worker():
            try:
                c = connect(self.db_path)
                try:
                    storage.merge_and_store(c, "E1", [("P1", "zh"), ("P1", "en")])
                finally:
                    c.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        with self.conn() as conn:
            detail = storage.get_exhibit(conn, "E1")
            self.assertEqual(detail["pair_count"], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM batch_log WHERE exhibit_no='E1' AND status='committed'"
            ).fetchone()[0], 6)

    def test_concurrent_interleaved_exhibits(self):
        """并发交错补录两个展品，各自结果独立且完整。"""
        errors: list[Exception] = []

        def worker(exhibit, plate, langs):
            try:
                c = connect(self.db_path)
                try:
                    storage.merge_and_store(c, exhibit, [(plate, x) for x in langs])
                finally:
                    c.close()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = []
        for i in range(6):
            exhibit = "EA" if i % 2 == 0 else "EB"
            threads.append(threading.Thread(
                target=worker, args=(exhibit, f"{exhibit}-P{i // 2}", ["zh", "en", "fr"])))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        with self.conn() as conn:
            for exhibit, plate_n in (("EA", 3), ("EB", 3)):
                d = storage.get_exhibit(conn, exhibit)
                self.assertEqual(d["storage_mode"], "dense")
                self.assertEqual(len(d["plates"]), plate_n)
                self.assertEqual(d["pair_count"], plate_n * 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
