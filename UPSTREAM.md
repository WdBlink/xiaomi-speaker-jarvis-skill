# Sources and attribution / 来源与致谢

This repository contains an original operational Skill and a sanitized reference implementation extracted from the author's Home Jarvis work. It does not redistribute speaker firmware, account credentials, household logs, or upstream application source. Dependencies keep their own licenses.

## Primary protocol and deployment references

- [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/): service calls, state queries, and bearer authentication.
- [Home Assistant Matter integration](https://www.home-assistant.io/integrations/matter/): supported installation path, commissioning, sharing, bridges, and network requirements. Self-managed containers need separate validation.
- [Home Assistant Linux installation](https://www.home-assistant.io/installation/linux/): choosing HA OS or Container for the target machine.
- [Matter.js server v1.5.5 Docker guide](https://github.com/matter-js/matterjs-server/blob/v1.5.5/docs/docker.md) and [WebSocket API](https://github.com/matter-js/matterjs-server/blob/v1.5.5/docs/websockets_api.md): the version examined in the originating deployment; not an instruction to assume that version is current forever.
- [MiniMax native tool guide](https://platform.minimax.io/docs/guides/text-m3-function-call) and [OpenAI-compatible API](https://platform.minimax.io/docs/api-reference/text-openai-api): complete assistant/tool message exchange, model parameters, and response metadata.
- [python-miio](https://github.com/rytilahti/python-miio): the dependency used for local device information and single-attempt commands. Runtime pins `0.5.12`; cached handshake timestamp behavior was tested on that version.
- [Xiaomi MIoT specification service](https://home.miot-spec.com/): a source of candidate capabilities, not proof that a specific firmware responds to every listed action.

## Community research used in the original investigation

- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor), examined at [c4db715dace9806e905153c2977608873e8ab7c9](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor/blob/c4db715dace9806e905153c2977608873e8ab7c9/token_extractor.py): authorized account-based token retrieval. Upstream code is not bundled here; inspect its current behavior and license before use.
- [xiaogpt](https://github.com/yihong0618/xiaogpt), examined at [4258b0b53cf6af062f1a01af993d47a161382a0f](https://github.com/yihong0618/xiaogpt/blob/4258b0b53cf6af062f1a01af993d47a161382a0f/xiaogpt/config.py): a reference for Xiaomi cloud conversation access. These are not documented stable public Xiaomi APIs.
- [xiaomi_ai_llm](https://github.com/open-speaker-llm/xiaomi_ai_llm): research context for a different route that requires system access and device-specific audio work. Its existence does not establish a non-invasive root path for the tested firmware.
- [REA](https://github.com/morluto/rea): a reverse-engineering workflow that motivated investigation. A research framework cannot itself supply a missing firmware exploit, recovery path, or device authorization.

## README structure

[release-it](https://github.com/release-it/release-it) informed the direct installation/usage entry. [VoltAgent](https://github.com/voltagent/voltagent) informed bilingual navigation and component boundaries, discovered through [awesome-readme](https://github.com/matiassingers/awesome-readme). No branding, screenshots, or product claims were copied.

Sources were consulted when preparing this package on 2026-10-27. Recheck version-sensitive behavior before deploying on another model or updating firmware. 本仓库保留成功路线和未解决问题，不将社区项目的能力或原案例的权限自动扩展到其他设备。
