"""
单元测试 - 选股系统的核心逻辑与网络健壮性

运行：
    python3 -m unittest -v test_stock_picker.py

说明：
- 默认全部为「离线」测试，用 mock 模拟 AKShare / 网络，不依赖外网。
- 末尾有一个「联网」测试 test_live_eastmoney_reachable，默认跳过；
  设置环境变量 RUN_LIVE_TESTS=1 时才会真正访问东方财富接口。
"""

import os
import unittest
from unittest import mock

import pandas as pd

import stock_picker as sp


class TestParsing(unittest.TestCase):
    def test_parse_ratio(self):
        self.assertEqual(sp.parse_ratio("12.5%"), 12.5)
        self.assertEqual(sp.parse_ratio("1,234.5%"), 1234.5)
        self.assertEqual(sp.parse_ratio("abc"), 0.0)

    def test_parse_amount(self):
        self.assertEqual(sp.parse_amount("1,000万"), 1000.0)
        self.assertEqual(sp.parse_amount("-500"), -500.0)
        self.assertEqual(sp.parse_amount(None), 0.0)

    def test_get_board(self):
        self.assertEqual(sp.get_board("600519"), "主板(沪)")
        self.assertEqual(sp.get_board("000001"), "主板(深)")
        self.assertEqual(sp.get_board("300750"), "创业板")
        self.assertEqual(sp.get_board("688981"), "科创板")
        self.assertEqual(sp.get_board("830799"), "北交所")


class TestProxyBypass(unittest.TestCase):
    def test_configure_network_clears_proxy_and_neutralizes_discovery(self):
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"

        # 模拟系统级代理被 requests 探测到
        import requests.utils as rqu
        rqu.getproxies = lambda: {"https": "http://127.0.0.1:7890"}

        with mock.patch.object(sp, "BYPASS_SYSTEM_PROXY", True):
            sp.configure_network()

        self.assertNotIn("HTTP_PROXY", os.environ)
        self.assertNotIn("HTTPS_PROXY", os.environ)
        self.assertIn("eastmoney.com", os.environ.get("NO_PROXY", ""))
        # 关键：requests 对 eastmoney 不再解析出任何代理
        resolved = rqu.get_environ_proxies(
            "https://push2.eastmoney.com/api/qt/clist/get", no_proxy=None
        )
        self.assertEqual(resolved, {})


class TestRetry(unittest.TestCase):
    def test_retry_succeeds_after_failures(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            return "ok"

        with mock.patch.object(sp, "DATA_FETCH_RETRIES", 3), \
             mock.patch.object(sp, "DATA_FETCH_RETRY_DELAY", 0):
            self.assertEqual(sp.fetch_with_retry("测试", flaky), "ok")
        self.assertEqual(calls["n"], 3)

    def test_retry_exhausts_and_raises(self):
        def always_fail():
            raise RuntimeError("net down")

        with mock.patch.object(sp, "DATA_FETCH_RETRIES", 2), \
             mock.patch.object(sp, "DATA_FETCH_RETRY_DELAY", 0):
            with self.assertRaises(RuntimeError):
                sp.fetch_with_retry("测试", always_fail)


class TestFetchSectorFlow(unittest.TestCase):
    def test_uses_correct_sector_type(self):
        """回归测试：必须用 '行业资金流' 而非 '行业资金流向'（旧 bug）"""
        captured = {}

        def fake(indicator, sector_type):
            captured["sector_type"] = sector_type
            return pd.DataFrame({"名称": ["银行"], "今日主力净流入-净额": [1.0]})

        with mock.patch.object(sp.ak, "stock_sector_fund_flow_rank", side_effect=fake), \
             mock.patch.object(sp, "DATA_FETCH_RETRY_DELAY", 0):
            df = sp.fetch_sector_flow()

        self.assertEqual(captured["sector_type"], "行业资金流")
        self.assertFalse(df.empty)

    def test_returns_empty_on_persistent_failure(self):
        def boom(*a, **k):
            raise RuntimeError("net")

        with mock.patch.object(sp.ak, "stock_sector_fund_flow_rank", side_effect=boom), \
             mock.patch.object(sp, "DATA_FETCH_RETRIES", 2), \
             mock.patch.object(sp, "DATA_FETCH_RETRY_DELAY", 0):
            df = sp.fetch_sector_flow()
        self.assertTrue(df.empty)


class TestDiagnostics(unittest.TestCase):
    def test_is_loopback(self):
        self.assertTrue(sp._is_loopback("127.0.0.1"))
        self.assertTrue(sp._is_loopback("::1"))
        self.assertFalse(sp._is_loopback("47.112.165.11"))

    def test_help_detects_tun_mode(self):
        """TCP 通但 HTTPS 失败 → 应判定为 TLS 层干扰（TUN/审查），而非接口问题"""
        diag = {
            "dns_ok": True, "resolved_ip": "47.112.165.11",
            "tcp_ok": True, "peer_ip": "47.112.165.11", "hijacked": False,
            "http_ok": False, "http_error": "ConnectionError: RemoteDisconnected",
        }
        with mock.patch.object(sp, "diagnose_connectivity", return_value=diag):
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sp.print_proxy_help()
            out = buf.getvalue()
        self.assertIn("TLS", out)
        self.assertIn("TUN", out)

    def test_help_treats_http_status_as_server_issue(self):
        """拿到 HTTP 状态码（如 502）→ 判定为服务端问题，不应误报代理/TLS"""
        diag = {
            "dns_ok": True, "resolved_ip": "47.112.165.11",
            "tcp_ok": True, "peer_ip": "47.112.165.11", "hijacked": False,
            "http_ok": False, "http_status": 502, "http_body_head": "<html>502",
        }
        with mock.patch.object(sp, "diagnose_connectivity", return_value=diag):
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sp.print_proxy_help()
            out = buf.getvalue()
        self.assertIn("502", out)
        self.assertIn("服务器", out)
        self.assertNotIn("TUN", out)

    def test_help_detects_loopback_proxy(self):
        diag = {
            "dns_ok": True, "resolved_ip": "47.112.165.11",
            "tcp_ok": True, "peer_ip": "127.0.0.1", "hijacked": True,
        }
        with mock.patch.object(sp, "diagnose_connectivity", return_value=diag):
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sp.print_proxy_help()
            out = buf.getvalue()
        self.assertIn("127.0.0.1", out)
        self.assertIn("直连", out)


# 用于「指定股票资金流向」测试的样本（代码、市场、名称）
SAMPLE_STOCKS = [
    ("600519", "sh", "贵州茅台"),
    ("000001", "sz", "平安银行"),
    ("300750", "sz", "宁德时代"),
    ("688981", "sh", "中芯国际"),
]


def _find_main_inflow_col(df: pd.DataFrame):
    """在个股资金流向 DataFrame 中找到“主力净流入”相关列"""
    for col in df.columns:
        if "主力" in str(col) and "净流入" in str(col):
            return col
    return None


class TestSpecificStocksFundFlowOffline(unittest.TestCase):
    """离线：用 mock 验证“按指定股票取资金流向”的封装逻辑（始终运行）"""

    def _fake_flow_df(self):
        return pd.DataFrame({
            "日期": ["2026-06-04", "2026-06-05", "2026-06-06",
                     "2026-06-09", "2026-06-10", "2026-06-11"],
            "主力净流入-净额": [1e7, -2e6, 3e7, 5e6, -1e6, 8e6],
            "收盘价": [1700, 1710, 1725, 1730, 1728, 1740],
        })

    def test_fetch_hist_flow_picks_correct_market(self):
        """600 开头 → sh，其它 → sz；并验证按指定股票调用 akshare"""
        captured = []

        def fake(stock, market):
            captured.append((stock, market))
            return self._fake_flow_df()

        with mock.patch.object(sp.ak, "stock_individual_fund_flow", side_effect=fake), \
             mock.patch.object(sp, "DATA_FETCH_RETRY_DELAY", 0):
            for code, expected_market, _name in SAMPLE_STOCKS:
                sp.fetch_hist_flow(code, days=5)

        self.assertEqual(captured, [
            ("600519", "sh"), ("000001", "sz"),
            ("300750", "sz"), ("688981", "sh"),
        ])

    def test_fetch_hist_flow_returns_last_n_days(self):
        with mock.patch.object(sp.ak, "stock_individual_fund_flow",
                               return_value=self._fake_flow_df()), \
             mock.patch.object(sp, "DATA_FETCH_RETRY_DELAY", 0):
            df = sp.fetch_hist_flow("600519", days=3)
        self.assertEqual(len(df), 3)
        self.assertEqual(df["日期"].tolist(), ["2026-06-09", "2026-06-10", "2026-06-11"])

    def test_fetch_hist_flow_empty_on_failure(self):
        with mock.patch.object(sp.ak, "stock_individual_fund_flow",
                               side_effect=RuntimeError("net")), \
             mock.patch.object(sp, "DATA_FETCH_RETRIES", 2), \
             mock.patch.object(sp, "DATA_FETCH_RETRY_DELAY", 0):
            df = sp.fetch_hist_flow("600519")
        self.assertTrue(df.empty)


class TestSpecificStocksFundFlowLive(unittest.TestCase):
    """
    联网：通过 AKShare 真正拉取指定几支股票的资金流向数据并校验。

    需设置 RUN_LIVE_TESTS=1 才运行；若当前网络/代理无法访问东方财富，
    测试会 skip（而非误报失败）。
        RUN_LIVE_TESTS=1 python3 -m unittest -v test_stock_picker.TestSpecificStocksFundFlowLive
    """

    @classmethod
    def setUpClass(cls):
        if os.environ.get("RUN_LIVE_TESTS") != "1":
            raise unittest.SkipTest("设置 RUN_LIVE_TESTS=1 才运行联网测试")
        sp.configure_network()

    def _fetch(self, code, market):
        return sp.fetch_with_retry(
            f"{code} 资金流向",
            sp.ak.stock_individual_fund_flow,
            stock=code, market=market,
        )

    def test_fetch_fund_flow_for_specific_stocks(self):
        results = {}
        for code, market, name in SAMPLE_STOCKS:
            try:
                df = self._fetch(code, market)
            except Exception as e:
                self.skipTest(f"网络/代理无法访问东方财富（{code} {name}）：{e}")

            with self.subTest(stock=f"{code} {name}"):
                self.assertIsInstance(df, pd.DataFrame)
                self.assertFalse(df.empty, f"{code} {name} 返回空数据")
                col = _find_main_inflow_col(df)
                self.assertIsNotNone(col, f"{code} {name} 缺少“主力净流入”列：{list(df.columns)}")
                # 数值列应可解析为数字
                vals = pd.to_numeric(
                    df[col].astype(str).str.replace(",", ""), errors="coerce"
                ).dropna()
                self.assertGreater(len(vals), 0, f"{code} {name} 主力净流入无有效数值")
                results[code] = len(df)

        self.assertTrue(results, "没有任何股票成功获取数据")
        print("\n  指定股票资金流向获取成功：", results)


class TestLive(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1",
                         "设置 RUN_LIVE_TESTS=1 才运行联网测试")
    def test_live_eastmoney_reachable(self):
        sp.configure_network()
        d = sp.diagnose_connectivity()
        self.assertTrue(d.get("dns_ok"), d)
        self.assertTrue(d.get("tcp_ok"), d)
        self.assertTrue(d.get("http_ok"), d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
