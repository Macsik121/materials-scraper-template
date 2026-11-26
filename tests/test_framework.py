from __future__ import annotations

import json
import py_compile
import tempfile
import unittest
import zipfile
from pathlib import Path

from adapters import list_adapters, load_adapter
from core.browser import SessionCircuitBreaker, SessionExpiredError, is_login_redirect
from core.downloader import decode_redirect_url, extract_pdfjs_file_url, has_signed_url_params, validate_magic_bytes
from core.fs_utils import PathTooLongError, build_item_path, is_subpath, sanitize_name
from core.handlers.test_readonly import is_destructive_control_text
from core.handlers.video_stream import inspect_stream_manifest
from core.models import Course, Item, ItemStatus, ItemType, Module
from core.state import ManifestStore, StateStore
from scripts.sanitize_capture import sanitize_content
from scripts.split_materials import split_course_materials
from scripts.verify_course import audit_directory_offline
from scripts.zip_folders import zip_single_folder

ROOT_DIR = Path(__file__).resolve().parent.parent


class FrameworkTestSuite(unittest.TestCase):
    def test_models_roundtrip(self) -> None:
        course = Course(
            course_id="sql-101",
            title="Основы СУБД",
            platform="moodle",
            source_url="https://lms.example.edu/course/1",
            modules=[
                Module(
                    module_id="m1",
                    order=1,
                    title="Введение",
                    items=[
                        Item(
                            item_id="i1",
                            order=1,
                            title="Конспект",
                            item_type=ItemType.DOCUMENT,
                            source_url="https://lms.example.edu/files/1.pdf",
                            extension=".pdf",
                        )
                    ],
                )
            ],
        )
        restored = Course.from_dict(course.to_dict())
        self.assertEqual(restored.course_id, "sql-101")
        self.assertEqual(restored.total_items, 1)
        self.assertEqual(restored.modules[0].items[0].item_type, ItemType.DOCUMENT)

    def test_fs_utils_windows_safety_and_max_path(self) -> None:
        self.assertEqual(sanitize_name('CON'), "_CON")
        self.assertEqual(sanitize_name('Лекция: часть 1 / 2?...'), "Лекция_ часть 1 _ 2_")

        base = Path("C:/Courses")
        p = build_item_path(
            base_dir=base,
            module_order=1,
            module_title="Очень длинное название модуля " * 6,
            item_order=12,
            item_type=ItemType.DOCUMENT,
            item_title="Очень длинное название лекции " * 6,
            extension=".pdf",
            total_modules=10,
            total_items=120,
        )
        self.assertLessEqual(len(str(p)), 260)
        self.assertIn("012_Doc_", p.name)

        too_long_base = Path("C:/Users/Student/" + ("segment_" * 35))
        with self.assertRaises(PathTooLongError):
            build_item_path(
                base_dir=too_long_base,
                module_order=1,
                module_title="Mod",
                item_order=1,
                item_type=ItemType.DOCUMENT,
                item_title="Doc",
                extension=".pdf",
            )

    def test_state_and_manifest_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = Course(course_id="c1", title="Test", platform="tpl", source_url="https://x")
            mstore = ManifestStore(root)
            mstore.save(course)
            self.assertEqual(mstore.load().title, "Test")

            sstore = StateStore(root)
            doc = root / "M01_Intro" / "01_Doc_Test.pdf"
            doc.parent.mkdir(parents=True)
            doc.write_bytes(b"%PDF-1.7\nhello\n%%EOF\n")
            sstore.set_status("i1", ItemStatus.DONE, local_path=doc, size_bytes=doc.stat().st_size)

            reloaded = StateStore(root)
            self.assertTrue(reloaded.should_skip("i1"))
            self.assertFalse(reloaded.should_skip("i1", force_recheck=True))
            self.assertEqual(reloaded.summary()["DONE"], 1)

    def test_magic_bytes_and_redirect_decoder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good_pdf = root / "ok.pdf"
            good_pdf.write_bytes(b"%PDF-1.7\nbody\n%%EOF\n")
            self.assertTrue(validate_magic_bytes(good_pdf).valid)

            fake_webm = root / "fake.webm"
            fake_webm.write_bytes(b"<!DOCTYPE html><html><body>Login</body></html>")
            res = validate_magic_bytes(fake_webm)
            self.assertFalse(res.valid)
            self.assertEqual(res.detected_type, "html_masquerade")

            good_webm = root / "ok.webm"
            good_webm.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 32)
            self.assertTrue(validate_magic_bytes(good_webm).valid)

        # Windows-1251 redirect shim (Postmortem §7)
        cp1251_url = "https://old-lms.ru/redirect.asp?url=https%3A%2F%2Fcdn.ru%2F%CB%E5%EA%F6%E8%FF.webm"
        decoded, enc = decode_redirect_url(cp1251_url)
        self.assertEqual(enc, "windows-1251")
        self.assertEqual(decoded, "https://cdn.ru/Лекция.webm")

        pdfjs = "https://lms.ru/pdfjs/web/viewer.html?file=https%3A%2F%2Flms.ru%2Fdocs%2Flec1.pdf"
        self.assertEqual(extract_pdfjs_file_url(pdfjs), "https://lms.ru/docs/lec1.pdf")
        self.assertTrue(has_signed_url_params("https://cdn.example.com/v.mp4?X-Amz-Expires=300&X-Amz-Signature=abc"))

    def test_drm_inspector_and_circuit_breaker(self) -> None:
        aes128 = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://lms.ru/key"\n'
        res_aes = inspect_stream_manifest(aes128)
        self.assertFalse(res_aes.is_drm)
        self.assertTrue(res_aes.is_aes128)

        widevine = '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"\n'
        res_drm = inspect_stream_manifest(widevine)
        self.assertTrue(res_drm.is_drm)
        self.assertGreaterEqual(len(res_drm.indicators), 2)

        saved = []
        cb = SessionCircuitBreaker(threshold=2, non_interactive=True, on_save_state=lambda: saved.append(True))
        cb.record_auth_failure("HTTP 401")
        self.assertEqual(len(saved), 0)
        with self.assertRaises(SessionExpiredError):
            cb.record_auth_failure("HTTP 403")
        self.assertEqual(len(saved), 1)
        self.assertTrue(is_login_redirect("https://lms.example.ru/auth/login"))
        self.assertTrue(is_destructive_control_text("Начать тест заново"))
        self.assertFalse(is_destructive_control_text("Показать пояснение к вопросу"))

    def test_adapter_registry(self) -> None:
        self.assertIn("_template", list_adapters(include_template=True))
        adapter = load_adapter("_template")
        self.assertEqual(adapter.platform_name, "template")

    def test_sanitize_capture_har_html_idempotent(self) -> None:
        har = json.dumps(
            {
                "log": {
                    "version": "1.2",
                    "entries": [
                        {
                            "request": {
                                "url": "https://lms.ru/lesson?token=secret123&id=5",
                                "headers": [{"name": "Cookie", "value": "sid=abc"}],
                                "postData": {
                                    "params": [
                                        {"name": "csrf_token", "value": "tok_999"},
                                        {"name": "user_email", "value": "a@b.edu"},
                                        {"name": "password", "value": "hunter2"},
                                    ]
                                },
                            },
                            "response": {
                                "headers": [{"name": "Set-Cookie", "value": "sid=xyz"}],
                                "content": {
                                    "text": '<input value="rev_csrf_1" name="_csrf_token" type="hidden"> +7 (916) 555-01-42'
                                },
                            },
                        }
                    ],
                }
            }
        )
        cleaned, fmt, stats = sanitize_content(har, fmt="auto")
        self.assertEqual(fmt, "har")
        self.assertGreater(stats.total, 0)
        self.assertNotIn("secret123", cleaned)
        self.assertNotIn("tok_999", cleaned)
        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn("rev_csrf_1", cleaned)
        self.assertNotIn("a@b.edu", cleaned)

        cleaned2, _, stats2 = sanitize_content(cleaned, fmt="har")
        self.assertEqual(stats2.total, 0)
        self.assertEqual(cleaned2, cleaned)

    def test_verify_split_and_zip_cli_core(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course_dir = root / "downloaded_course"
            mod_dir = course_dir / "M01_Intro"
            mod_dir.mkdir(parents=True)

            pdf = mod_dir / "01_Doc_Notes.pdf"
            pdf.write_bytes(b"%PDF-1.7\ncontent\n%%EOF\n")
            bad_pdf = mod_dir / "02_Doc_Broken.pdf"
            bad_pdf.write_bytes(b"<html>Login</html>")
            junk = mod_dir / "03_Vid_Part.webm.crdownload"
            junk.write_bytes(b"partial")

            checked, broken, junk_files, _ = audit_directory_offline(course_dir, clean=True)
            self.assertEqual(checked, 2)
            self.assertEqual(len(broken), 1)
            self.assertEqual(len(junk_files), 1)
            self.assertFalse(bad_pdf.exists())
            self.assertFalse(junk.exists())
            self.assertTrue(pdf.exists())

            # split_materials: must reject --out inside --dir (even via ..)
            with self.assertRaises(ValueError):
                split_course_materials(course_dir, course_dir / "sub" / ".." / "sorted")

            out_sorted = root / "sorted_course"
            counts = split_course_materials(course_dir, out_sorted, flat=True)
            self.assertEqual(counts["documents"], 1)
            self.assertTrue((out_sorted / "documents" / "M01_Intro__01_Doc_Notes.pdf").exists())

            # zip_folders: smart compression + Zip64
            archives_dir = root / "archives"
            status, zpath, count, size = zip_single_folder(mod_dir, archives_dir)
            self.assertEqual(status, "ok")
            self.assertEqual(count, 1)
            self.assertGreater(size, 0)
            with zipfile.ZipFile(zpath, "r") as zf:
                names = zf.namelist()
                self.assertEqual(names, ["M01_Intro/01_Doc_Notes.pdf"])

    def test_ps1_has_utf8_bom_and_all_py_compile(self) -> None:
        ps1_bytes = (ROOT_DIR / "scrape-course.ps1").read_bytes()
        self.assertTrue(ps1_bytes.startswith(b"\xef\xbb\xbf"), "scrape-course.ps1 обязан иметь UTF-8 BOM")

        py_files = [
            p
            for p in ROOT_DIR.rglob("*.py")
            if "node_modules" not in p.parts and ".venv" not in p.parts
        ]
        self.assertGreaterEqual(len(py_files), 18)
        for py_file in py_files:
            py_compile.compile(str(py_file), doraise=True)


if __name__ == "__main__":
    unittest.main()
