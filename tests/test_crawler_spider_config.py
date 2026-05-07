"""
测试 SentinelSpider/config.py — Settings 配置类

覆盖字段默认值、.env 加载、extra = "allow" 行为
"""

from pathlib import Path

project_root = Path(__file__).parent.parent

import sys
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "tools" / "SentinelSpider"))

import pytest


class TestSettingsDefaults:
    """测试 Settings 默认值"""

    def test_default_db_host(self):
        """DB_HOST 默认值"""
        from config import Settings
        s = Settings(_env_file=None)
        assert s.DB_HOST == "your_host"

    def test_default_db_port(self):
        """DB_PORT 默认值"""
        from config import Settings
        s = Settings(_env_file=None)
        assert s.DB_PORT == 3306

    def test_default_db_name(self):
        """DB_NAME 默认值"""
        from config import Settings
        s = Settings(_env_file=None)
        assert s.DB_NAME == "mindspider"

    def test_default_db_charset(self):
        """DB_CHARSET 默认值"""
        from config import Settings
        s = Settings(_env_file=None)
        assert s.DB_CHARSET == "utf8mb4"

    def test_default_mindspider_api_key_none(self):
        """MINDSPIDER_API_KEY 默认为 None"""
        from config import Settings
        s = Settings(_env_file=None)
        assert s.MINDSPIDER_API_KEY is None

    def test_default_mindspider_base_url(self):
        """MINDSPIDER_BASE_URL 默认值"""
        from config import Settings
        s = Settings(_env_file=None)
        assert s.MINDSPIDER_BASE_URL == "https://api.deepseek.com"

    def test_default_mindspider_model_name(self):
        """MINDSPIDER_MODEL_NAME 默认值"""
        from config import Settings
        s = Settings(_env_file=None)
        assert s.MINDSPIDER_MODEL_NAME == "deepseek-chat"

    def test_default_db_dialect(self):
        """DB_DIALECT 默认值"""
        from config import Settings
        s = Settings(_env_file=None)
        assert s.DB_DIALECT == "mysql"


class TestSettingsExtraAllow:
    """测试 extra = "allow" 行为"""

    def test_extra_field_accepted(self):
        """未知字段不会导致验证错误（extra=allow）"""
        from config import Settings
        s = Settings(_env_file=None, UNKNOWN_FIELD="value")
        # pydantic v2 extra=allow 将未知字段存在 __pydantic_extra__ 中
        assert s.__pydantic_extra__ is not None
        assert s.__pydantic_extra__["UNKNOWN_FIELD"] == "value"

    def test_env_vars_loaded(self, monkeypatch):
        """环境变量可以被读取"""
        monkeypatch.setenv("DB_HOST", "test-host")
        from config import Settings
        s = Settings()
        assert s.DB_HOST == "test-host"

    def test_env_var_override(self, monkeypatch):
        """环境变量覆盖默认值"""
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_DIALECT", "postgresql")
        from config import Settings
        s = Settings()
        assert s.DB_PORT == 5432
        assert s.DB_DIALECT == "postgresql"

    def test_env_var_sentinel_spider_keys(self, monkeypatch):
        """SENTINEL_SPIDER_* 不是 Settings 定义字段，env var 不会自动映射"""
        monkeypatch.setenv("SENTINEL_SPIDER_API_KEY", "sk-test")
        from config import Settings
        s = Settings(_env_file=None)
        # pydantic v2: extra=allow 只影响构造函数入参，不影响环境变量
        # SENTINEL_SPIDER_API_KEY 没有对应字段，不会被加载
        assert not hasattr(s, "SENTINEL_SPIDER_API_KEY")

    def test_env_var_mindspider_keys(self, monkeypatch):
        """MINDSPIDER_* 字段通过标准 pydantic 字段映射读取"""
        monkeypatch.setenv("MINDSPIDER_API_KEY", "sk-mindspider")
        from config import Settings
        s = Settings()
        assert s.MINDSPIDER_API_KEY == "sk-mindspider"

    def test_mindspider_vs_sentinel_spider_naming(self, monkeypatch):
        """记录 MINDSPIDER_* 与 SENTINEL_SPIDER_* 的命名差异（已知问题）"""
        monkeypatch.setenv("SENTINEL_SPIDER_API_KEY", "sk-sentinel")
        monkeypatch.setenv("MINDSPIDER_API_KEY", "sk-mindspider")
        from config import Settings
        s = Settings(_env_file=None)
        # MINDSPIDER_* 是定义字段，env var 正常映射
        assert s.MINDSPIDER_API_KEY == "sk-mindspider"
        # SENTINEL_SPIDER_* 不是定义字段，pydantic v2 不会自动映射 env var
        # SentinelSpider/main.py 的 check_config() 会检查 SENTINEL_SPIDER_*
        # 但 config.py 定义的字段是 MINDSPIDER_* — 这是命名不一致问题
        assert not hasattr(s, "SENTINEL_SPIDER_API_KEY")
