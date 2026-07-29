#!/usr/bin/env python3
"""Fund-safety tests for skills/verdikta-hunter/verdikta_exec.py.

Run: python3 tests/test_verdikta_exec.py

The executor is imported directly and exercised against a scripted fake bounty
API + fake Base RPC. `eth_account` and `requests` are stubbed (stdlib-only — CI
installs neither), signing returns inspectable fake bytes, and nothing touches
the network. These tests pin the guarantees this pack's safety claim rests on:

  - transactions are refused unless `to` == the pinned BountyEscrow contract
    and chainId == 8453, and value <= VERDIKTA_MAX_SPEND_ETH
  - the start value must equal the API's parsed.ethMaxBudget
  - a cap refusal after the prepare tx leaves a PREPARED_INCOMPLETE state entry
    so the next run cannot double-submit
  - dry-run hits only /submit/dry-run: no RPC calls, no signing, no state
  - the happy path calls bundle -> complete -> confirm in order, broadcasts
    exactly two txs, and records state + daily spend count
  - per-run (1) and per-day caps drop excess submissions before any API call
  - caps resolve env > memory/verdikta-hunter.env > defaults
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills" / "verdikta-hunter" / "verdikta_exec.py"

ESCROW = "0x2Ae271f5E86bee449a36B943414b7C1a7b39772D"
PREPAY_OK = 240_000_000_000_000    # 0.00024 ETH — realistic worst-case prepay
PREPAY_OVER = 600_000_000_000_000  # 0.0006 ETH — over the 0.0005 default cap


# ── stub third-party deps before the module loads ─────────────────────
def _no_net(*a, **k):
    raise AssertionError("network reached without a stub")


fake_requests = types.ModuleType("requests")
fake_requests.request = _no_net
fake_requests.post = _no_net
sys.modules["requests"] = fake_requests


class _FakeSigned:
    def __init__(self, payload):
        self.raw_transaction = b"\x02" + payload


class _FakeAccount:
    address = "0x00000000000000000000000000000000DeaDBeef"
    key = b"\x01" * 32

    @staticmethod
    def sign_transaction(tx, key):
        # encode the tx dict so tests can decode exactly what was signed
        return _FakeSigned(json.dumps({k: str(v) for k, v in tx.items()}).encode())

    @staticmethod
    def from_key(_):
        return _FakeAccount()


fake_eth = types.ModuleType("eth_account")
fake_eth.Account = _FakeAccount
sys.modules["eth_account"] = fake_eth

os.environ["VERDIKTA_API_KEY"] = "test-key-not-real"

_spec = importlib.util.spec_from_file_location("verdikta_exec", MODULE_PATH)
vx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vx)


class FakeBackend:
    """Scripted bounty API + Base JSON-RPC."""

    def __init__(self, prepay=PREPAY_OK, escrow=ESCROW, chain_id=8453,
                 start_value=None, balance=5_000_000_000_000_000, receipt_status="0x1"):
        self.prepay, self.escrow, self.chain_id = prepay, escrow, chain_id
        self.start_value = prepay if start_value is None else start_value
        self.balance, self.receipt_status = balance, receipt_status
        self.api_calls, self.rpc_calls, self.sent_raw = [], [], []
        self.api_kwargs = {}

    def api(self, method, path, **kw):
        self.api_calls.append(path)
        self.api_kwargs = kw
        if path.endswith("/submit/dry-run"):
            return {"success": True, "valid": True, "errors": []}
        if path.endswith("/submit/bundle"):
            return {"hunterCid": "QmTest", "transactions": [
                {"to": self.escrow, "data": "0xab", "value": "0",
                 "chainId": self.chain_id, "gasLimit": 1000000}]}
        if path.endswith("/submit/bundle/complete"):
            return {"parsed": {"submissionId": 7, "evalWallet": "0x" + "11" * 20,
                               "ethMaxBudget": str(self.prepay)},
                    "transactions": [
                        {"to": self.escrow, "data": "0xcd", "value": str(self.start_value),
                         "chainId": self.chain_id, "gasLimit": 4000000}]}
        if path.endswith("/submissions/confirm"):
            return {"success": True}
        if path.endswith("/finalize"):
            return {"transaction": {"to": self.escrow, "data": "0xef", "value": "0",
                                    "chainId": self.chain_id, "gasLimit": 300000}}
        raise AssertionError(f"unexpected API call {method} {path}")

    def rpc(self, method, params):
        self.rpc_calls.append(method)
        if method == "eth_gasPrice":
            return hex(10_000_000)
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_getTransactionCount":
            return hex(len(self.sent_raw))
        if method == "eth_getTransactionReceipt":
            return {"status": self.receipt_status}
        if method == "eth_sendRawTransaction":
            self.sent_raw.append(params[0])
            return "0x" + f"{len(self.sent_raw):064x}"
        raise AssertionError(f"unexpected RPC {method}")

    def signed_tx(self, i):
        """Decode what sign_transaction actually received for broadcast i."""
        return json.loads(bytes.fromhex(self.sent_raw[i][2:])[1:].decode())


def make_request(job_id=97, dry_run=False, action="submit", **extra):
    files_dir = Path(f".pending-verdikta/files/{job_id}")
    files_dir.mkdir(parents=True, exist_ok=True)
    (files_dir / "report.md").write_text("# test\n")
    req = {"action": action, "jobId": job_id, "files": ["report.md"], "dryRun": dry_run}
    req.update(extra)
    path = Path(f".pending-verdikta/{action}-{job_id}.json")
    path.write_text(json.dumps(req))
    return req, path


class VerdiktaSafetyTests(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        Path(".pending-verdikta").mkdir()
        self.acct = _FakeAccount()
        for k in list(vx.DEFAULTS):
            os.environ.pop(k, None)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()
        for k in list(vx.DEFAULTS):
            os.environ.pop(k, None)

    def _exec(self, backend):
        ex = vx.Executor(vx.load_caps())
        ex.api, ex.rpc = backend.api, backend.rpc
        return ex

    def _state(self):
        return json.loads(Path("memory/state/verdikta-hunter.json").read_text())

    # ── happy path ───────────────────────────────────────────────────

    def test_happy_path(self):
        be = FakeBackend()
        ex = self._exec(be)
        req, path = make_request()
        ex.do_submit(req, self.acct, path.parent)
        self.assertEqual(be.api_calls, ["/jobs/97/submit/bundle",
                                        "/jobs/97/submit/bundle/complete",
                                        "/jobs/97/submissions/confirm"])
        self.assertEqual(len(be.sent_raw), 2)
        start = be.signed_tx(1)
        self.assertEqual(start["to"].lower(), ESCROW.lower())
        self.assertEqual(int(start["value"]), PREPAY_OK)
        self.assertEqual(int(start["chainId"]), 8453)
        state = self._state()
        self.assertEqual(state["submissions"]["97:7"]["status"], "PENDING_EVALUATION")
        self.assertNotIn("97:?", state["submissions"])
        self.assertEqual(sum(state["daily"].values()), 1)

    # ── the safety envelope ──────────────────────────────────────────

    def test_overcap_prepay_refused_after_prepare(self):
        be = FakeBackend(prepay=PREPAY_OVER)
        ex = self._exec(be)
        req, path = make_request()
        with self.assertRaises(vx.CapRefused):
            ex.do_submit(req, self.acct, path.parent)
        self.assertEqual(len(be.sent_raw), 1)  # prepare only — start never signed
        self.assertNotIn("/jobs/97/submissions/confirm", be.api_calls)
        self.assertEqual(self._state()["submissions"]["97:?"]["status"],
                         "PREPARED_INCOMPLETE")

    def test_wrong_destination_refused_before_any_tx(self):
        be = FakeBackend(escrow="0x" + "99" * 20)
        ex = self._exec(be)
        req, path = make_request()
        with self.assertRaises(vx.CapRefused):
            ex.do_submit(req, self.acct, path.parent)
        self.assertEqual(be.sent_raw, [])

    def test_wrong_chain_refused_before_any_tx(self):
        be = FakeBackend(chain_id=1)
        ex = self._exec(be)
        req, path = make_request()
        with self.assertRaises(vx.CapRefused):
            ex.do_submit(req, self.acct, path.parent)
        self.assertEqual(be.sent_raw, [])

    def test_start_value_must_match_parsed_budget(self):
        be = FakeBackend(start_value=PREPAY_OK + 1)
        ex = self._exec(be)
        req, path = make_request()
        with self.assertRaises(vx.CapRefused):
            ex.do_submit(req, self.acct, path.parent)
        self.assertEqual(len(be.sent_raw), 1)

    def test_insufficient_balance_refused(self):
        be = FakeBackend(balance=1000)
        ex = self._exec(be)
        req, path = make_request()
        with self.assertRaises(RuntimeError) as cm:
            ex.do_submit(req, self.acct, path.parent)
        self.assertIn("insufficient balance", str(cm.exception))
        self.assertEqual(be.sent_raw, [])

    def test_gas_price_capped(self):
        be = FakeBackend()
        be.rpc_gas_override = True
        ex = self._exec(be)
        # gasPrice*2 would be 0.02 gwei here; cap is 3 gwei -> uses the doubled value
        req, path = make_request()
        ex.do_submit(req, self.acct, path.parent)
        self.assertEqual(int(be.signed_tx(0)["maxFeePerGas"]), 20_000_000)

    def test_tx_revert_detected(self):
        be = FakeBackend(receipt_status="0x0")
        ex = self._exec(be)
        req, path = make_request()
        with self.assertRaises(RuntimeError) as cm:
            ex.do_submit(req, self.acct, path.parent)
        self.assertIn("REVERTED", str(cm.exception))

    # ── dry-run ──────────────────────────────────────────────────────

    def test_dry_run_touches_nothing(self):
        be = FakeBackend()
        ex = self._exec(be)
        req, path = make_request(dry_run=True)
        ex.do_submit(req, None, path.parent)
        self.assertEqual(be.api_calls, ["/jobs/97/submit/dry-run"])
        self.assertEqual(be.rpc_calls, [])
        self.assertFalse(Path("memory/state/verdikta-hunter.json").exists())
        hunter = be.api_kwargs.get("data", {}).get("hunter", "")
        self.assertRegex(hunter, r"^0x[a-fA-F0-9]{40}$")

    def test_dry_run_uses_wallet_address_when_available(self):
        be = FakeBackend()
        ex = self._exec(be)
        req, path = make_request(dry_run=True)
        ex.do_submit(req, self.acct, path.parent)
        self.assertEqual(be.api_kwargs.get("data", {}).get("hunter"), self.acct.address)

    # ── finalize ─────────────────────────────────────────────────────

    def test_finalize(self):
        be = FakeBackend()
        ex = self._exec(be)
        ex.do_finalize({"action": "finalize", "jobId": 97, "submissionId": 7}, self.acct)
        self.assertEqual(be.api_calls, ["/jobs/97/submissions/7/finalize"])
        self.assertEqual(len(be.sent_raw), 1)
        self.assertEqual(int(be.signed_tx(0)["value"]), 0)
        self.assertEqual(self._state()["submissions"]["97:7"]["status"], "FINALIZED")

    # ── caps resolution ──────────────────────────────────────────────

    def test_caps_default_when_unset(self):
        caps = vx.load_caps()
        self.assertEqual(caps["VERDIKTA_MAX_SPEND_ETH"], "0.0005")
        self.assertEqual(caps["VERDIKTA_MAX_SUBMISSIONS_PER_DAY"], "5")

    def test_caps_file_overrides_default(self):
        Path("memory").mkdir(exist_ok=True)
        Path("memory/verdikta-hunter.env").write_text(
            "# operator overrides\nVERDIKTA_MAX_SPEND_ETH=0.001\nVERDIKTA_MAX_SUBMISSIONS_PER_DAY=2\n")
        caps = vx.load_caps()
        self.assertEqual(caps["VERDIKTA_MAX_SPEND_ETH"], "0.001")
        self.assertEqual(caps["VERDIKTA_MAX_SUBMISSIONS_PER_DAY"], "2")

    def test_env_beats_caps_file(self):
        Path("memory").mkdir(exist_ok=True)
        Path("memory/verdikta-hunter.env").write_text("VERDIKTA_MAX_SPEND_ETH=0.001\n")
        os.environ["VERDIKTA_MAX_SPEND_ETH"] = "0.002"
        self.assertEqual(vx.load_caps()["VERDIKTA_MAX_SPEND_ETH"], "0.002")

    def test_lower_cap_from_file_is_enforced(self):
        Path("memory").mkdir(exist_ok=True)
        Path("memory/verdikta-hunter.env").write_text("VERDIKTA_MAX_SPEND_ETH=0.0001\n")
        be = FakeBackend()  # prepay 0.00024 — now over the tightened cap
        ex = self._exec(be)
        req, path = make_request()
        with self.assertRaises(vx.CapRefused):
            ex.do_submit(req, self.acct, path.parent)

    # ── queue-level rate limits (main()) ─────────────────────────────

    def test_daily_cap_drops_submit_before_any_api_call(self):
        Path("memory/state").mkdir(parents=True, exist_ok=True)
        Path("memory/state/verdikta-hunter.json").write_text(
            json.dumps({"submissions": {}, "daily": {vx.utc_today(): 5}}))
        make_request()
        be = FakeBackend()
        os.environ["VERDIKTA_WALLET_KEY"] = "0x" + "11" * 32
        try:
            orig = vx.Executor.__init__
            vx.Executor.__init__ = lambda s, caps: (orig(s, caps), setattr(s, "api", be.api),
                                                    setattr(s, "rpc", be.rpc))[0]
            rc = vx.main()
        finally:
            vx.Executor.__init__ = orig
            os.environ.pop("VERDIKTA_WALLET_KEY", None)
        self.assertEqual(rc, 0)
        self.assertEqual(be.api_calls, [])   # never reached the API
        self.assertEqual(be.sent_raw, [])

    def test_queue_cleared_after_run(self):
        make_request(dry_run=True)
        be = FakeBackend()
        orig = vx.Executor.__init__
        try:
            vx.Executor.__init__ = lambda s, caps: (orig(s, caps), setattr(s, "api", be.api),
                                                    setattr(s, "rpc", be.rpc))[0]
            vx.main()
        finally:
            vx.Executor.__init__ = orig
        self.assertEqual(list(Path(".pending-verdikta").glob("*.json")), [])

    def test_no_pending_is_clean_noop(self):
        self.assertEqual(vx.main(), 0)

    # ── dependency bootstrap ─────────────────────────────────────────

    def test_ensure_deps_short_circuits_when_importable(self):
        calls = []
        orig = vx.subprocess.run
        vx.subprocess.run = lambda *a, **k: calls.append(a) or orig(["true"])
        try:
            self.assertTrue(vx.ensure_deps())   # stubs are importable in this suite
        finally:
            vx.subprocess.run = orig
        self.assertEqual(calls, [], "pip must not run when deps already import")

    def test_ensure_deps_fails_when_install_succeeds_but_import_does_not(self):
        """Regression: pip exit 0 is not proof the module is importable in THIS
        process (a fresh install can land off sys.path). Verified live on run
        30479053009, where the old code reported success and then crashed with
        ModuleNotFoundError."""
        class _Ok:
            returncode, stderr, stdout = 0, "", ""
        orig_run, orig_check = vx.subprocess.run, vx._deps_importable
        vx.subprocess.run = lambda *a, **k: _Ok()
        vx._deps_importable = lambda: False          # never becomes importable
        try:
            self.assertFalse(vx.ensure_deps(), "must not claim success on an unusable install")
        finally:
            vx.subprocess.run, vx._deps_importable = orig_run, orig_check

    def test_ensure_deps_succeeds_once_import_works_after_install(self):
        class _Ok:
            returncode, stderr, stdout = 0, "", ""
        seen = {"n": 0}

        def _importable():
            seen["n"] += 1
            return seen["n"] > 1                     # fails first, works post-install
        orig_run, orig_check = vx.subprocess.run, vx._deps_importable
        vx.subprocess.run = lambda *a, **k: _Ok()
        vx._deps_importable = _importable
        try:
            self.assertTrue(vx.ensure_deps())
        finally:
            vx.subprocess.run, vx._deps_importable = orig_run, orig_check


if __name__ == "__main__":
    unittest.main(verbosity=2)
