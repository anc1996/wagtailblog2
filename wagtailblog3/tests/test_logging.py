import logging
import logging.config
import ast
import tempfile
from pathlib import Path
from unittest.mock import Mock

from django.test import SimpleTestCase

from observability import (
    LOG_DOMAINS,
    get_email_debug_config,
    get_logging_config,
    get_performance_logging_config,
)
from observability.filters import MaxLevelFilter, ModuleFilter, ProjectRelativePathFilter
from observability.helpers import get_context_logger, log_exceptions
from observability.registry import (
    LOG_DIRECTORIES,
    LOG_FILE_BY_KEY,
    LOG_FILE_CATALOG,
    LOG_FILE_SPECS,
    handler_name,
    resolve_domain,
)


class LoggingConfigTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp_dir.name)
        self.config = get_logging_config(log_dir=self.log_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_creates_all_configured_log_directories(self):
        expected = set(LOG_DIRECTORIES)
        self.assertEqual(
            {path.name for path in self.log_dir.iterdir() if path.is_dir()}, expected
        )

    def test_each_application_has_activity_and_error_routes(self):
        for domain in LOG_DOMAINS:
            for logger_name in domain.logger_names:
                with self.subTest(logger_name=logger_name):
                    logger = self.config["loggers"][logger_name]
                    self.assertIn(handler_name(domain, "activity"), logger["handlers"])
                    self.assertIn(handler_name(domain, "error"), logger["handlers"])
                    self.assertFalse(logger["propagate"])
                    error_handler = self.config["handlers"][handler_name(domain, "error")]
                    self.assertEqual(error_handler["level"], "ERROR")

    def test_base_email_loggers_keep_base_error_route(self):
        for logger_name in ("base.models", "base.tasks", "base.utils", "base.rate_limit"):
            with self.subTest(logger_name=logger_name):
                handlers = self.config["loggers"][logger_name]["handlers"]
                self.assertIn("domain_base_error", handlers)
                self.assertIn("email_error_file", handlers)

    def test_root_is_an_error_file_safety_net(self):
        self.assertEqual(
            self.config["root"],
            {"handlers": ["console", "fallback_error_file"], "level": "WARNING"},
        )
        self.assertEqual(
            self.config["handlers"]["fallback_error_file"]["level"], "ERROR"
        )

    def test_warning_and_error_files_are_separate(self):
        django_handlers = self.config["loggers"]["django"]["handlers"]
        self.assertIn("django_warning_file", django_handlers)
        self.assertIn("django_error_file", django_handlers)
        self.assertEqual(
            self.config["handlers"]["django_warning_file"]["filters"],
            ["project_relative_path", "sensitive_data", "max_warning"],
        )

    def test_catalog_and_all_file_handlers_are_exactly_consistent(self):
        handlers = dict(self.config["handlers"])
        handlers.update(get_email_debug_config(log_dir=self.log_dir)["handlers"])
        handlers.update(get_performance_logging_config(log_dir=self.log_dir)["handlers"])
        file_handlers = {
            name: handler for name, handler in handlers.items() if "filename" in handler
        }

        self.assertEqual(set(file_handlers), {spec.handler for spec in LOG_FILE_SPECS})
        self.assertEqual(len(LOG_FILE_CATALOG), len(LOG_FILE_SPECS))
        self.assertEqual(
            len({spec.relative_path for spec in LOG_FILE_SPECS}), len(LOG_FILE_SPECS)
        )
        for spec in LOG_FILE_SPECS:
            with self.subTest(spec=spec.key):
                handler = file_handlers[spec.handler]
                self.assertEqual(
                    Path(handler["filename"]).relative_to(self.log_dir).as_posix(),
                    spec.relative_path,
                )
                self.assertEqual(handler["backupCount"], spec.backup_count)
                self.assertEqual(handler["maxBytes"], spec.max_bytes)

    def test_config_does_not_contain_log_path_literals(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "apps"
            / "observability"
            / "config.py"
        )
        tree = ast.parse(path.read_bytes())
        log_path_literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.endswith(".log")
        ]
        self.assertEqual(log_path_literals, [])

    def test_email_debug_uses_three_rotations_from_catalog(self):
        handler = get_email_debug_config(log_dir=self.log_dir)["handlers"][
            "email_debug_file"
        ]
        self.assertEqual(handler["backupCount"], 3)
        self.assertEqual(
            handler["backupCount"], LOG_FILE_BY_KEY["email_debug"].backup_count
        )

    def test_file_handlers_are_process_safe(self):
        file_handlers = [
            handler
            for handler in self.config["handlers"].values()
            if "filename" in handler
        ]
        self.assertTrue(file_handlers)
        self.assertTrue(
            all(
                handler["class"]
                == "concurrent_log_handler.ConcurrentRotatingFileHandler"
                for handler in file_handlers
            )
        )

    def test_project_components_have_dedicated_domains(self):
        expected = {
            "wagtailblog3.mongo": "mongo/mongo_error.log",
            "wagtailblog3.mongodb": "mongo/mongo_error.log",
            "wagtailblog3.ai_backends": "ai/ai_error.log",
            "wagtailblog3.storage_backends": "storage/storage_error.log",
        }
        for logger_name, suffix in expected.items():
            with self.subTest(logger_name=logger_name):
                error_handlers = [
                    self.config["handlers"][name]
                    for name in self.config["loggers"][logger_name]["handlers"]
                    if name.endswith("_error")
                ]
                self.assertEqual(len(error_handlers), 1)
                self.assertTrue(error_handlers[0]["filename"].endswith(suffix))

    def test_real_records_are_written_to_their_domain_only(self):
        self.config["handlers"]["console"]["level"] = "CRITICAL"
        logging.config.dictConfig(self.config)
        try:
            logger = logging.getLogger("wagtailblog3.mongo")
            logger.info("mongo activity probe")
            try:
                raise RuntimeError("mongo error probe")
            except RuntimeError:
                logger.exception("mongo failed")
            for handler in logging.getLogger("wagtailblog3.mongo").handlers:
                handler.flush()

            activity = (self.log_dir / "mongo/mongo.log").read_text(encoding="utf-8")
            errors = (self.log_dir / "mongo/mongo_error.log").read_text(encoding="utf-8")
            self.assertIn("mongo activity probe", activity)
            self.assertNotIn("mongo failed", activity)
            self.assertIn("mongo failed", errors)
            self.assertIn("Traceback", errors)
            self.assertFalse((self.log_dir / "system/error.log").exists())
        finally:
            logging.shutdown()

    def test_every_registered_domain_really_writes_its_own_error_file(self):
        self.config["handlers"]["console"]["level"] = "CRITICAL"
        logging.config.dictConfig(self.config)
        try:
            markers = {}
            for domain in LOG_DOMAINS:
                marker = f"route-probe-{domain.key}"
                markers[domain.key] = marker
                logger = logging.getLogger(domain.logger_names[0])
                logger.error(marker)
                for handler in logger.handlers:
                    handler.flush()

            for domain in LOG_DOMAINS:
                error_path = self.log_dir / LOG_FILE_BY_KEY[
                    f"{domain.key}_error"
                ].relative_path
                content = error_path.read_text(encoding="utf-8")
                self.assertIn(markers[domain.key], content)
                for other_key, other_marker in markers.items():
                    if other_key != domain.key:
                        self.assertNotIn(other_marker, content)
            self.assertFalse((self.log_dir / "system/error.log").exists())
        finally:
            logging.shutdown()

    def test_base_email_error_is_written_once_to_each_intentional_domain(self):
        self.config["handlers"]["console"]["level"] = "CRITICAL"
        logging.config.dictConfig(self.config)
        try:
            logger = logging.getLogger("base.models")
            logger.error("base-email-cross-domain-probe")
            for handler in logger.handlers:
                handler.flush()

            base_error = (self.log_dir / "base/base_error.log").read_text(encoding="utf-8")
            email_error = (self.log_dir / "email/email_error.log").read_text(encoding="utf-8")
            self.assertEqual(base_error.count("base-email-cross-domain-probe"), 1)
            self.assertEqual(email_error.count("base-email-cross-domain-probe"), 1)
            self.assertFalse((self.log_dir / "system/error.log").exists())
        finally:
            logging.shutdown()

    def test_django_warning_and_error_really_use_separate_files(self):
        self.config["handlers"]["console"]["level"] = "CRITICAL"
        logging.config.dictConfig(self.config)
        try:
            logger = logging.getLogger("django.request")
            logger.warning("django-warning-probe")
            logger.error("django-error-probe")
            for handler in logging.getLogger("django").handlers:
                handler.flush()

            warnings = (self.log_dir / "system/django_warning.log").read_text(encoding="utf-8")
            errors = (self.log_dir / "system/django_error.log").read_text(encoding="utf-8")
            self.assertIn("django-warning-probe", warnings)
            self.assertNotIn("django-error-probe", warnings)
            self.assertIn("django-error-probe", errors)
            self.assertFalse((self.log_dir / "system/error.log").exists())
        finally:
            logging.shutdown()

    def test_django_server_writes_only_to_runtime_catalog_file(self):
        self.config["handlers"]["console"]["level"] = "CRITICAL"
        logging.config.dictConfig(self.config)
        try:
            logging.getLogger("django.server").info('"GET /probe/ HTTP/1.1" 200 2')
            logging.getLogger("django.request").warning("ordinary-django-warning")
            for logger_name in ("django.server", "django"):
                for handler in logging.getLogger(logger_name).handlers:
                    handler.flush()

            runtime = (self.log_dir / "runtime/runserver.log").read_text(encoding="utf-8")
            warnings = (self.log_dir / "system/django_warning.log").read_text(encoding="utf-8")
            self.assertIn("GET /probe/", runtime)
            self.assertNotIn("ordinary-django-warning", runtime)
            self.assertNotIn("GET /probe/", warnings)
            self.assertNotIn("runtime_runserver_file", self.config["loggers"]["django"]["handlers"])
            self.assertNotIn("runtime_runserver_file", self.config["root"]["handlers"])
        finally:
            logging.shutdown()

    def test_runtime_handler_keeps_only_five_rotations(self):
        self.config["handlers"]["console"]["level"] = "CRITICAL"
        runtime = self.config["handlers"]["runtime_runserver_file"]
        runtime["maxBytes"] = 256
        logging.config.dictConfig(self.config)
        try:
            logger = logging.getLogger("django.server")
            for index in range(80):
                logger.info("runtime-rotation-%03d %s", index, "x" * 80)
            for handler in logger.handlers:
                handler.flush()
        finally:
            logging.shutdown()

        path = self.log_dir / "runtime/runserver.log"
        self.assertTrue(path.exists())
        self.assertTrue(all(Path(f"{path}.{rotation}").exists() for rotation in range(1, 6)))
        self.assertFalse(Path(f"{path}.6").exists())

    def test_writing_redacts_secrets_and_absolute_project_path(self):
        self.config["handlers"]["console"]["level"] = "CRITICAL"
        logging.config.dictConfig(self.config)
        try:
            logger = logging.getLogger("blog.security")
            try:
                raise RuntimeError("Authorization: Bearer raw-token")
            except RuntimeError:
                logger.exception(
                    "password=hunter2 Cookie=sessionid=abc api_key=plain-key"
                )
            for handler in logger.handlers:
                handler.flush()
        finally:
            logging.shutdown()

        content = (self.log_dir / "blog/blog_error.log").read_text(encoding="utf-8")
        for secret in ("hunter2", "raw-token", "sessionid=abc", "plain-key"):
            self.assertNotIn(secret, content)
        self.assertIn("[REDACTED]", content)
        self.assertNotIn(str(Path(__file__).resolve().parents[2]), content)
        self.assertIn("wagtailblog3/tests/test_logging.py", content)


class LoggingFilterTests(SimpleTestCase):
    def test_module_filter_uses_logger_namespace_boundaries(self):
        filter_instance = ModuleFilter(["base"])
        self.assertTrue(filter_instance.filter(logging.LogRecord("base", 20, "", 0, "", (), None)))
        self.assertTrue(
            filter_instance.filter(logging.LogRecord("base.models", 20, "", 0, "", (), None))
        )
        self.assertFalse(
            filter_instance.filter(logging.LogRecord("baseball", 20, "", 0, "", (), None))
        )

    def test_max_level_filter_excludes_errors(self):
        filter_instance = MaxLevelFilter("WARNING")
        self.assertTrue(
            filter_instance.filter(logging.LogRecord("test", logging.WARNING, "", 0, "", (), None))
        )
        self.assertFalse(
            filter_instance.filter(logging.LogRecord("test", logging.ERROR, "", 0, "", (), None))
        )

    def test_project_path_filter_never_returns_parent_segments(self):
        root = Path(__file__).resolve().parents[2]
        filter_instance = ProjectRelativePathFilter(root)
        project_record = logging.LogRecord(
            "test", logging.INFO, str(root / "wagtailblog3/apps/blog/views.py"), 1, "", (), None
        )
        external_record = logging.LogRecord(
            "test", logging.INFO, "/usr/lib/python3/site-packages/vendor/client.py", 1, "", (), None
        )
        filter_instance.filter(project_record)
        filter_instance.filter(external_record)
        self.assertEqual(project_record.relative_path, "wagtailblog3/apps/blog/views.py")
        self.assertEqual(external_record.relative_path, "client.py")
        self.assertNotIn("..", project_record.relative_path)


class LoggingHelperTests(SimpleTestCase):
    def test_log_exceptions_records_traceback_and_reraises(self):
        logger = Mock()

        @log_exceptions(logger=logger, message="operation failed")
        def fail():
            raise ValueError("bad value")

        with self.assertRaisesRegex(ValueError, "bad value"):
            fail()

        logger.log.assert_called_once()
        args, kwargs = logger.log.call_args
        self.assertEqual(args[:3], (logging.ERROR, "%s: %s", "operation failed"))
        self.assertTrue(kwargs["exc_info"])

    def test_context_logger_supports_standard_logger_methods(self):
        with self.assertLogs("blog.context", level="INFO") as captured:
            logger = get_context_logger("blog.context", page_id=42)
            logger.info("saved")

        self.assertIn("saved - Context: {'page_id': 42}", captured.output[0])


class LoggingConventionTests(SimpleTestCase):
    project_root = Path(__file__).resolve().parents[1]

    def _source_logger_name(self, path):
        relative = path.relative_to(self.project_root)
        if relative.parts[0] == "apps":
            parts = relative.parts[1:]
        else:
            parts = ("wagtailblog3", *relative.parts)
        return ".".join((*parts[:-1], Path(parts[-1]).stem))

    def test_every_project_module_logger_has_a_registered_domain(self):
        missing = []
        for path in self.project_root.rglob("*.py"):
            if "tests" in path.parts or "observability" in path.parts:
                continue
            tree = ast.parse(path.read_bytes())
            uses_module_logger = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getLogger"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "__name__"
                for node in ast.walk(tree)
            )
            if uses_module_logger:
                logger_name = self._source_logger_name(path)
                if resolve_domain(logger_name) is None:
                    missing.append(logger_name)
        self.assertEqual(missing, [], f"未注册日志域: {missing}")

    def test_error_calls_in_except_blocks_preserve_tracebacks(self):
        violations = []
        for path in self.project_root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_bytes())
            for handler in ast.walk(tree):
                if not isinstance(handler, ast.ExceptHandler):
                    continue
                for node in ast.walk(handler):
                    is_logger_error = (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "error"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "logger"
                    )
                    if is_logger_error and not any(
                        keyword.arg == "exc_info" for keyword in node.keywords
                    ):
                        violations.append(f"{path.relative_to(self.project_root)}:{node.lineno}")
        self.assertEqual(violations, [], f"ERROR 未保留 traceback: {violations}")

    def test_business_logging_does_not_record_post_or_email_bodies(self):
        forbidden_names = {"plain_message", "html_message", "email_body", "message_body"}
        violations = []
        for path in self.project_root.rglob("*.py"):
            if "tests" in path.parts or "observability" in path.parts:
                continue
            tree = ast.parse(path.read_bytes())
            for node in ast.walk(tree):
                is_log_call = (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"debug", "info", "warning", "error", "exception", "critical"}
                )
                if not is_log_call:
                    continue
                names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
                source = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
                if names & forbidden_names or "request.POST" in source:
                    violations.append(f"{path.relative_to(self.project_root)}:{node.lineno}")
        self.assertEqual(violations, [], f"业务日志记录了 POST 或邮件正文: {violations}")
