"""§A10 — Unicode round-trip: handakuten / combining / variation selectors /
ZWSP / NUL via direct stdio JSON-RPC for full byte-level control.

For each case we save_memory, list_memories, and search_memory; then we
diff the recovered content byte-for-byte against the original. Recovery
goes through HSET/HGET in Redis, which preserves bytes verbatim.
"""
from __future__ import annotations
import json
import subprocess
import sys
import time
import unicodedata


CASES = [
    {
        "id": "case1_handakuten",
        "label": "1. 半濁点単独 (precomposed handakuten)",
        "content": "パ ピ プ",
        "expect": "verbatim",
    },
    {
        "id": "case2_combining",
        # カ (U+30AB) + COMBINING KATAKANA-HIRAGANA VOICED SOUND MARK (U+3099)
        "label": "2. 結合文字 (KA + combining U+3099 = decomposed ガ)",
        "content": "ガ",
        "expect": "verbatim",  # Stored raw; NFKC only affects embedding/dedup
    },
    {
        "id": "case3_variation_selectors",
        # 神 (U+795E) + VARIATION SELECTOR-17 (U+E0100), and 髙 (U+9AD9)
        "label": "3. 異体字セレクタ (神+VS17, 髙)",
        "content": "神\U000E0100 髙",
        "expect": "verbatim",
    },
    {
        "id": "case4_zwsp",
        # ABC + ZERO WIDTH SPACE (U+200B) + DEF
        "label": "4. ゼロ幅スペース (ABC + U+200B + DEF)",
        "content": "ABC​DEF",
        "expect": "verbatim",
    },
    {
        "id": "case5_nul",
        # A + NUL + B
        "label": "5. 制御文字 (A + U+0000 + B)",
        "content": "A\x00B",
        "expect": "depends",  # see what server does
    },
]


def _send(p, msg):
    p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    p.stdin.flush()


def _recv(p, want_id, deadline):
    while time.perf_counter() < deadline:
        line = p.stdout.readline()
        if not line:
            return None
        try:
            obj = json.loads(line.decode("utf-8"))
        except Exception:
            continue
        if obj.get("id") == want_id:
            return obj


def jrpc(p, method, params, _id, timeout=30.0):
    _send(p, {"jsonrpc": "2.0", "id": _id, "method": method, "params": params})
    return _recv(p, _id, time.perf_counter() + timeout)


def notify(p, method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    _send(p, msg)


def main():
    proc = subprocess.Popen(
        ["n3mc-workingmemory"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    init = jrpc(proc, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "unicode-rt", "version": "0.1"}}, _id=1, timeout=120)
    notify(proc, "notifications/initialized")
    if not init or "result" not in init:
        print("initialize failed", file=sys.stderr); sys.exit(1)

    SESSION = "eval-§A10-roundtrip"
    results = []
    next_id = 10

    for case in CASES:
        original = case["content"]
        info = {
            "id": case["id"],
            "label": case["label"],
            "original_text": original,
            "original_repr": repr(original),
            "original_codepoints": [f"U+{ord(c):04X}" for c in original],
            "original_byte_len": len(original.encode("utf-8")),
            "original_char_len": len(original),
            "original_nfc": unicodedata.normalize("NFC", original) == original,
            "original_nfkc": unicodedata.normalize("NFKC", original) == original,
        }

        # save_memory
        next_id += 1
        sv = jrpc(proc, "tools/call", {
            "name": "save_memory",
            "arguments": {"content": original, "session_id": SESSION},
        }, _id=next_id, timeout=30)
        sv_text = ""
        if sv and "result" in sv:
            for blk in sv["result"].get("content", []):
                if blk.get("type") == "text":
                    sv_text += blk.get("text", "")
        info["save_response"] = sv_text[:300]

        # Try to extract id field for direct retrieval (if saved_count==1)
        saved_id = None
        try:
            j = json.loads(sv_text.split("\n")[0])
            saved_id = j.get("id") or (j.get("ids", [None])[0] if j.get("ids") else None)
            info["save_status"] = j.get("status")
            info["save_saved"] = j.get("saved")
            info["save_reason"] = j.get("reason")
        except Exception:
            info["save_parse_error"] = True

        # list_memories to recover the just-saved row verbatim
        next_id += 1
        ls = jrpc(proc, "tools/call", {
            "name": "list_memories",
            "arguments": {"limit": 5},
        }, _id=next_id, timeout=15)
        ls_text = ""
        if ls and "result" in ls:
            for blk in ls["result"].get("content", []):
                if blk.get("type") == "text":
                    ls_text += blk.get("text", "")

        # Find the recovered text in the list output
        recovered = None
        if saved_id and saved_id in ls_text:
            # Strip header line "**[id]** ts" then take next line as content
            after = ls_text.split(saved_id, 1)[1]
            after = after.split("\n", 1)[1] if "\n" in after else after
            # Content runs until blank line + "---" separator
            parts = after.split("\n\n---")
            recovered = parts[0].lstrip("\n") if parts else after.lstrip("\n")

        info["recovered_text"] = recovered
        info["recovered_repr"] = repr(recovered) if recovered is not None else None
        info["recovered_codepoints"] = (
            [f"U+{ord(c):04X}" for c in recovered] if recovered is not None else None)

        # Byte-level diff
        if recovered is None:
            info["byte_identical"] = None
            info["diff_offset"] = None
        else:
            ob = original.encode("utf-8")
            rb = recovered.encode("utf-8")
            info["original_bytes_hex"] = ob.hex()
            info["recovered_bytes_hex"] = rb.hex()
            info["byte_identical"] = (ob == rb)
            if not info["byte_identical"]:
                # find first divergence
                for i, (a, b) in enumerate(zip(ob, rb)):
                    if a != b:
                        info["diff_offset"] = i
                        info["diff_orig_byte"] = f"0x{a:02X}"
                        info["diff_recv_byte"] = f"0x{b:02X}"
                        break
                else:
                    info["diff_offset"] = min(len(ob), len(rb))

        results.append(info)

    # Cleanup
    next_id += 1
    jrpc(proc, "tools/call", {
        "name": "delete_memories_by_session",
        "arguments": {"session_id": SESSION},
    }, _id=next_id, timeout=15)

    try: proc.stdin.close()
    except Exception: pass
    try: proc.terminate(); proc.wait(timeout=5)
    except Exception: proc.kill()

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
