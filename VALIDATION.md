# Validation / 验证范围

## Public package

The extracted runtime was checked on Python 3.12.13 with **250 tests passing, zero skipped**. Its suite uses synthetic fixtures, mocked cloud/device transports, and a loopback HA test server. The imported package was verified to come from the bundled `assets/runtime/src`, not from the original working tree. [Machine-readable result](skills/xiaomi-speaker-jarvis/assets/runtime/verification.json).

Reproduce locally from the repository root:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e skills/xiaomi-speaker-jarvis/assets/runtime
.venv/bin/python scripts/validate_package.py
.venv/bin/python -m unittest discover -s skills/xiaomi-speaker-jarvis/assets/runtime/tests -v
```

These checks do not authenticate to Xiaomi or MiniMax, commission a Matter device, operate real HA entities, or play audio. Passing them cannot establish hardware compatibility.

## Originating hardware experience

The Skill was extracted from a 2026-10-27 deployment involving an S12 speaker, a Mac bridge, Raspberry Pi Home Assistant, an Aqara M2, and MiniMax-M3. That source project passed 255 automated checks, including unrelated components omitted from this package. It also included physical device tests, account login, actual model tool calls, HA state reads, and LAN TTS requests.

Reported physical results are historical experience, not newly repeated tests of this public package. Raw household logs, device/account identifiers, credentials, QR codes, and databases were deliberately excluded. This limits external reproducibility of the historical claims; readers can reproduce the documented procedures on their own authorized hardware.

An earlier stage had user confirmation of wake/recognition while muted and of a single audible answer. The final tool migration used direct text and a synthetic fresh-record injection through the actual main entry with real model, HA, and speaker adapters; it did not repeat the final physical-microphone/listening check. One validation harness's final private-attribute lookup failed after playback reached idle; service restoration and subsequent standby volume were checked separately. Do not count that harness as an unqualified clean end-to-end run.

## Known unresolved results

- Two real curtain utterances failed during the first native-tool process. Their original causes were not logged. Later read-only replay, including a closed-curtain fixture, and main-entry validation succeeded. Sanitized model error diagnostics were added, but no specific root cause was proven.
- A failure on a second model request may still leave `model_calls=1` in the failure receipt. Do not use that field for precise billing statistics.
- Native Xiao Ai speech may overlap during Jarvis's audible window. Muting does not disable the stock assistant's interpretation or actions.
- An accepted TTS request may still fail to start or be missed by playback monitoring. Unknown writes and playback requests are not automatically retried.
- Root, raw PCM, a replacement wake word, firmware downgrade/recovery, and long-duration unattended reliability were not established.

## Release checks

The publication process additionally checks local links, syntax, skill frontmatter, package import paths, bilingual README consistency, private identifiers, and source-to-guide consistency. CI runs the offline checks on Linux and macOS. Review the actual workflow result for a given commit; the presence of a workflow file is not evidence that it passed.

A fresh repository-local virtual environment installed the package and passed all 250 tests. An independent walkthrough also copied the runtime into a temporary directory, confirmed its import path, ran the same suite, and checked that repeated configuration initialization preserved existing files and private permissions. The first miIO token extraction recipe was reviewed against pinned upstream source; no fresh Xiaomi login was performed for publication.

中文说明：公开包的 250 项测试与原工程的 255 项检查是不同范围；都不能换算为实机场景数量。真实家庭信息未随仓库公开，历史故障未被包装成“已彻底修复”。
