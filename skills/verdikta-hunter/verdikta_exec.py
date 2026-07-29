#!/usr/bin/env python3
"""Deterministic executor for Verdikta bounty requests queued by verdikta-hunter.

Invoked by the skill as its FINAL in-run action:

    python3 skills/verdikta-hunter/verdikta_exec.py

The skill writes request files to .pending-verdikta/ and never signs anything
itself; this script performs the authed API calls and every transaction
signature. The wallet key is read from this process's environment — it never
appears on the model's command line.

Fund-safety envelope (enforced HERE, client-side — the API response is treated
as untrusted input):
  - never signs a tx whose `to` != the pinned BountyEscrow contract, or whose
    chainId != 8453 (Base)
  - never signs a tx whose value exceeds VERDIKTA_MAX_SPEND_ETH (default 0.0005)
  - the start tx value must equal the API's own parsed.ethMaxBudget
  - at most ONE new submission per invocation; VERDIKTA_MAX_SUBMISSIONS_PER_DAY
    per UTC day (default 5), tracked in memory/state/verdikta-hunter.json
  - balance preflight (value + gas) before every tx; receipts checked for revert
  - a mid-flow failure leaves a PREPARED_INCOMPLETE state entry so the next run
    cannot double-submit to the same bounty

Cap overrides live in memory/verdikta-hunter.env (KEY=VALUE lines, committed to
git — every cap change is a visible diff). Real env vars win over that file.

Runs on stock Aeon: the write tier already grants Bash(python3:*), so this needs
no core allowlist change. Exit codes: 0 ok · 1 error · 3 refused by a safety cap.
"""
import datetime
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

PENDING_DIR = Path(".pending-verdikta")
STATE_FILE = Path("memory/state/verdikta-hunter.json")
CAPS_FILE = Path("memory/verdikta-hunter.env")

API = "https://bounties.verdikta.org/api"
ESCROW = "0x2Ae271f5E86bee449a36B943414b7C1a7b39772D"
CHAIN_ID = 8453
GAS_LIMITS = {"prepare": 1_000_000, "start": 4_000_000, "finalize": 300_000}

DEFAULTS = {
    "VERDIKTA_MAX_SPEND_ETH": "0.0005",
    "VERDIKTA_MAX_SUBMISSIONS_PER_DAY": "5",
    "VERDIKTA_MAX_GAS_GWEI": "3",
    "VERDIKTA_RPC_URL": "https://mainnet.base.org",
}


class CapRefused(Exception):
    """A safety cap refused a transaction. Never retried, never worked around."""


def load_caps():
    """Resolve caps: real env wins, then memory/verdikta-hunter.env, then defaults."""
    file_vals = {}
    if CAPS_FILE.exists():
        for line in CAPS_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            file_vals[k.strip()] = v.strip().strip('"').strip("'")
    resolved = {}
    for key, default in DEFAULTS.items():
        resolved[key] = os.environ.get(key) or file_vals.get(key) or default
    return resolved


def ensure_deps():
    """Install the two runtime deps if absent (mirrors vuln-scanner's in-run staging)."""
    try:
        import eth_account  # noqa: F401
        import requests  # noqa: F401
        return True
    except ImportError:
        print("verdikta-exec: installing python deps (eth-account, requests)...")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "eth-account", "requests"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"::warning::verdikta-exec: dep install failed: {r.stderr[:300]}")
            return False
        return True


# ── state + logging ──────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"submissions": {}, "daily": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE_FILE)


def utc_today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def log_line(msg):
    path = Path(f"memory/logs/{utc_today()}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "### verdikta-hunter (exec)"
    text = path.read_text() if path.exists() else ""
    if header not in text:
        text += f"\n{header}\n"
    text += f"- {msg}\n"
    path.write_text(text)


class Executor:
    def __init__(self, caps):
        import requests  # imported after ensure_deps()
        self._requests = requests
        self.rpc_url = caps["VERDIKTA_RPC_URL"]
        self.cap_wei = int(Decimal(caps["VERDIKTA_MAX_SPEND_ETH"]) * 10**18)
        self.max_gas_wei = int(Decimal(caps["VERDIKTA_MAX_GAS_GWEI"]) * 10**9)
        self.headers = {"X-Bot-API-Key": os.environ["VERDIKTA_API_KEY"]}

    # ── transport ────────────────────────────────────────────────────────

    def api(self, method, path, **kw):
        r = self._requests.request(method, f"{API}{path}", headers=self.headers, timeout=90, **kw)
        if not r.ok:
            raise RuntimeError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def rpc(self, method, params):
        r = self._requests.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            timeout=30,
        )
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"RPC {method}: {data['error']}")
        return data["result"]

    # ── the safety envelope ──────────────────────────────────────────────

    def guard_tx(self, tx, kind):
        """Refuse anything outside the envelope. `tx` is API-returned (untrusted)."""
        if str(tx.get("to", "")).lower() != ESCROW.lower():
            raise CapRefused(f"{kind}: tx.to {tx.get('to')} != pinned escrow {ESCROW}")
        if int(tx.get("chainId", CHAIN_ID)) != CHAIN_ID:
            raise CapRefused(f"{kind}: chainId {tx.get('chainId')} != {CHAIN_ID}")
        value = int(tx.get("value") or 0)
        if value > self.cap_wei:
            raise CapRefused(
                f"{kind}: tx value {value} wei ({value/1e18:.6f} ETH) exceeds "
                f"VERDIKTA_MAX_SPEND_ETH cap {self.cap_wei} wei ({self.cap_wei/1e18:.6f} ETH)")
        return value

    def sign_and_send(self, acct, tx, kind):
        from eth_account import Account
        value = self.guard_tx(tx, kind)
        gas_price = min(int(self.rpc("eth_gasPrice", []), 16) * 2, self.max_gas_wei)
        gas_limit = int(tx.get("gasLimit") or GAS_LIMITS[kind])
        balance = int(self.rpc("eth_getBalance", [acct.address, "latest"]), 16)
        needed = value + gas_limit * gas_price
        if balance < needed:
            raise RuntimeError(
                f"{kind}: insufficient balance — have {balance/1e18:.6f} ETH, "
                f"need {needed/1e18:.6f} ETH (value + gas)")
        nonce = int(self.rpc("eth_getTransactionCount", [acct.address, "pending"]), 16)
        signed = Account.sign_transaction({
            "to": tx["to"], "value": value, "data": tx["data"], "chainId": CHAIN_ID,
            "nonce": nonce, "gas": gas_limit, "maxFeePerGas": gas_price,
            "maxPriorityFeePerGas": min(gas_price, 10**8), "type": 2,
        }, acct.key)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self.rpc("eth_sendRawTransaction", ["0x" + raw.hex().removeprefix("0x")])
        print(f"  {kind} tx sent: {tx_hash}")
        deadline = time.time() + 180
        while time.time() < deadline:
            receipt = self.rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                if receipt.get("status") != "0x1":
                    raise RuntimeError(f"{kind} tx {tx_hash} REVERTED")
                return tx_hash
            time.sleep(3)
        raise RuntimeError(f"{kind} tx {tx_hash} not confirmed within 180s")

    # ── actions ──────────────────────────────────────────────────────────

    def do_finalize(self, req, acct):
        job_id, sub_id = req["jobId"], req["submissionId"]
        print(f"verdikta-exec: finalize #{job_id}/{sub_id}")
        resp = self.api("POST", f"/jobs/{job_id}/submissions/{sub_id}/finalize",
                        json={"hunter": acct.address})
        tx_hash = self.sign_and_send(acct, resp["transaction"], "finalize")
        state = load_state()
        entry = state["submissions"].setdefault(
            f"{job_id}:{sub_id}", {"jobId": job_id, "submissionId": sub_id})
        entry.update({"finalizeTx": tx_hash, "status": "FINALIZED"})
        save_state(state)
        log_line(f"Finalized #{job_id}/{sub_id}: {tx_hash}")

    def do_submit(self, req, acct, pending_dir):
        job_id = req["jobId"]
        files_dir = pending_dir / "files" / str(job_id)
        file_list = [(name, (files_dir / name).read_text()) for name in req["files"]]
        multipart = [("files", (n, c, "text/markdown")) for n, c in file_list]

        if req.get("dryRun"):
            print(f"verdikta-exec: DRY-RUN validate #{job_id}")
            # valid-format placeholder keeps the API's hunter check meaningful
            # when no wallet key is configured
            hunter = acct.address if acct else "0x0000000000000000000000000000000000000001"
            resp = self.api("POST", f"/jobs/{job_id}/submit/dry-run",
                            files=multipart, data={"hunter": hunter})
            print(f"  dry-run result: {json.dumps(resp)[:500]}")
            verdict = "VALID" if resp.get("valid") else "INVALID"
            issues = "; ".join(e.get("code", "?") for e in resp.get("errors") or [])
            log_line(f"Dry-run #{job_id}: {verdict}"
                     f"{' (' + issues + ')' if issues else ''} — no transactions sent")
            return

        print(f"verdikta-exec: submit #{job_id} as {acct.address}")
        data = {
            "hunterAddress": acct.address,
            "addendum": req.get("addendum", ""),
            "alpha": str(req.get("alpha", 200)),
            "maxOracleFee": str(req.get("maxOracleFee", "0.00002")),
            "estimatedBaseCost": str(req.get("estimatedBaseCost", "0.00001")),
            "maxFeeBasedScaling": str(req.get("maxFeeBasedScaling", 3)),
        }
        bundle = self.api("POST", f"/jobs/{job_id}/submit/bundle", files=multipart, data=data)
        hunter_cid = bundle["hunterCid"]
        step1_hash = self.sign_and_send(acct, bundle["transactions"][0], "prepare")

        # From here the submission exists on-chain — record it even if a later
        # stage fails, so the next run sees it and never double-submits.
        state = load_state()
        state["submissions"][f"{job_id}:?"] = {
            "jobId": job_id, "submissionId": None, "prepareTx": step1_hash,
            "startTx": None, "status": "PREPARED_INCOMPLETE",
            "submittedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "finalizeTx": None,
        }
        save_state(state)

        complete = self.api("POST", f"/jobs/{job_id}/submit/bundle/complete",
                            json={"txHash": step1_hash})
        parsed = complete["parsed"]
        sub_id = parsed["submissionId"]
        eth_max_budget = int(parsed["ethMaxBudget"])
        start_tx = complete["transactions"][0]
        if int(start_tx.get("value") or 0) != eth_max_budget:
            raise CapRefused(
                f"start: tx value {start_tx.get('value')} != parsed.ethMaxBudget {eth_max_budget}")
        # cap-check BEFORE confirm so a refusal costs nothing further
        self.guard_tx(start_tx, "start")

        self.api("POST", f"/jobs/{job_id}/submissions/confirm", json={
            "submissionId": sub_id, "hunter": acct.address,
            "hunterCid": hunter_cid, "evalWallet": parsed["evalWallet"],
        })

        step2_hash = self.sign_and_send(acct, start_tx, "start")

        state = load_state()
        state["submissions"].pop(f"{job_id}:?", None)  # replace the provisional entry
        state["submissions"][f"{job_id}:{sub_id}"] = {
            "jobId": job_id, "submissionId": sub_id,
            "prepareTx": step1_hash, "startTx": step2_hash,
            "ethMaxBudgetWei": str(eth_max_budget),
            "submittedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "PENDING_EVALUATION", "finalizeTx": None,
        }
        state["daily"][utc_today()] = state["daily"].get(utc_today(), 0) + 1
        save_state(state)
        log_line(f"Submitted #{job_id}/{sub_id}: prepare {step1_hash}, start {step2_hash} "
                 f"(prepay {eth_max_budget/1e18:.6f} ETH, refundable at finalize)")


def run_request(ex, req_path, acct):
    """Execute one queued request. Returns True when a real submission was sent."""
    req = json.loads(req_path.read_text())
    action = req.get("action")
    try:
        if action == "finalize":
            ex.do_finalize(req, acct)
        elif action == "submit":
            ex.do_submit(req, acct, req_path.parent)
            return not req.get("dryRun")
        else:
            print(f"::warning::verdikta-exec: unknown action '{action}' in {req_path.name}")
    except CapRefused as e:
        print(f"::warning::verdikta-exec: SAFETY CAP REFUSED — {e}")
        log_line(f"REFUSED by safety cap: {e}")
        raise
    return False


def main():
    pending = sorted(PENDING_DIR.glob("*.json")) if PENDING_DIR.is_dir() else []
    if not pending:
        print("verdikta-exec: no pending requests")
        return 0
    if not os.environ.get("VERDIKTA_API_KEY"):
        print("verdikta-exec: VERDIKTA_API_KEY not set, skipping")
        return 0
    if not ensure_deps():
        return 1

    caps = load_caps()
    max_per_day = int(caps["VERDIKTA_MAX_SUBMISSIONS_PER_DAY"])
    sent_today = load_state().get("daily", {}).get(utc_today(), 0)

    from eth_account import Account
    wallet_key = os.environ.get("VERDIKTA_WALLET_KEY")
    acct = Account.from_key(wallet_key) if wallet_key else None

    ex = Executor(caps)
    refused = False
    submitted_this_run = 0

    # Finalizes first — they only reclaim/settle escrow, never new spend.
    finalizes = [p for p in pending if p.name.startswith("finalize-")]
    submits = [p for p in pending if p.name.startswith("submit-")]

    for req_path in finalizes:
        if not wallet_key:
            print(f"::warning::verdikta-exec: VERDIKTA_WALLET_KEY not set — skipping {req_path.name}")
            continue
        print(f"verdikta-exec: processing {req_path.name}...")
        try:
            run_request(ex, req_path, acct)
        except CapRefused:
            refused = True
        except Exception as e:  # noqa: BLE001 — one bad request must not kill the queue
            print(f"::warning::verdikta-exec: {req_path.name} failed: {e}")

    for req_path in submits:
        is_dry = json.loads(req_path.read_text()).get("dryRun", False)
        if not is_dry:
            if not wallet_key:
                print(f"::warning::verdikta-exec: VERDIKTA_WALLET_KEY not set — skipping {req_path.name}")
                continue
            if submitted_this_run >= 1:
                print(f"::warning::verdikta-exec: per-run cap (1 submission) — dropping {req_path.name}")
                continue
            if sent_today >= max_per_day:
                print(f"::warning::verdikta-exec: daily cap ({max_per_day}) reached — dropping {req_path.name}")
                continue
        print(f"verdikta-exec: processing {req_path.name}...")
        try:
            if run_request(ex, req_path, acct):
                submitted_this_run += 1
                sent_today += 1
        except CapRefused:
            refused = True
        except Exception as e:  # noqa: BLE001
            print(f"::warning::verdikta-exec: {req_path.name} failed: {e}")

    for req_path in pending:
        req_path.unlink(missing_ok=True)
    print("verdikta-exec: done")
    return 3 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
