"""启动入口与路径锚定的回归测试。

守的是两条真实踩过的坑（都属于「换个人/换个 IDE 就起不来」，HTTP 级测试看不见）：

1. `main.py` 是模块不是脚本：直接 `python src/app/main.py`（PyCharm 点绿三角生成的
   运行配置就是这个）过去只会定义 `app = create_app()` 然后**退出码 0 静默结束**，
   表现为「后端无法启动」。现在补了 `run()` 入口，这里钉住它确实把配置传给了 uvicorn。
2. SQLite 的相对路径按**进程工作目录**解析：工作目录是 `src/app` 时，`./data/goalfin.db`
   会变成 `src/app/data/goalfin.db` —— 静默连到一个空库。现在在配置层锚定到项目根。
"""

from __future__ import annotations

import uvicorn

from app import main as main_module
from app.core.config import BASE_DIR, Settings


class TestSqlitePathAnchoring:
    """数据库 URL 的「相对路径锚定」规则。"""

    def test_relative_sqlite_url_is_anchored_to_project_root(self) -> None:
        cfg = Settings(database_url="sqlite+aiosqlite:///./data/goalfin.db")
        expected = (BASE_DIR / "data" / "goalfin.db").resolve().as_posix()
        assert cfg.database_url == f"sqlite+aiosqlite:///{expected}"
        # 锚定后一定是绝对路径：与进程工作目录无关
        assert cfg.database_url.startswith("sqlite+aiosqlite:////") or ":/" in cfg.database_url

    def test_memory_database_is_untouched(self) -> None:
        cfg = Settings(database_url="sqlite+aiosqlite:///:memory:")
        assert cfg.database_url == "sqlite+aiosqlite:///:memory:"

    def test_absolute_sqlite_url_is_untouched(self) -> None:
        posix = "sqlite+aiosqlite:////tmp/goalfin.db"
        assert Settings(database_url=posix).database_url == posix
        windows = "sqlite+aiosqlite:///D:/tmp/goalfin.db"
        assert Settings(database_url=windows).database_url == windows

    def test_non_sqlite_url_is_untouched(self) -> None:
        pg = "postgresql+asyncpg://user:pwd@localhost:5432/goalfin"
        assert Settings(database_url=pg).database_url == pg


class TestRunEntryPoint:
    """`python -m app.main` / 直接执行 main.py 时，启动参数必须来自配置。"""

    def test_run_passes_settings_to_uvicorn(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def fake_run(app: str, **kwargs: object) -> None:
            captured["app"] = app
            captured.update(kwargs)

        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr(main_module.settings, "app_host", "127.0.0.1")
        monkeypatch.setattr(main_module.settings, "app_port", 8123)
        monkeypatch.setattr(main_module.settings, "app_debug", False)

        main_module.run()

        # 必须是 import string：reload 模式要靠它在子进程里重新导入
        assert captured["app"] == "app.main:app"
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 8123
        assert captured["reload"] is False

    def test_run_enables_reload_and_src_watch_dir_in_debug(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def fake_run(app: str, **kwargs: object) -> None:
            captured["app"] = app
            captured.update(kwargs)

        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr(main_module.settings, "app_debug", True)

        main_module.run()

        assert captured["reload"] is True
        # 工作目录可能是 src/app（IDE 默认），必须显式把 src 目录加进监视范围
        assert captured["reload_dirs"] == [str(BASE_DIR / "src")]
