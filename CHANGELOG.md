# Changelog

All notable changes to the Kingdee Portal DSH plugin are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [1.0.4] - 2026-11-17

### Fixed
- Push (`下推`) to generate an expense reimbursement bill no longer reuses the
  auto-retrying API helper: Push is a non-idempotent write, and a transport
  timeout could leave Kingdee having created the bill while the portal reported
  failure (or created duplicates). The portal now sends the request with
  retries disabled and, whenever the result cannot be confirmed, raises an
  explicit "下推结果未确认" error that asks the user to check the bill list
  before retrying. (Ported from the public-edition fix of 2026-11-16.)
- After a successful push the source application bill's contact unit
  (往来单位) is inherited onto the new reimbursement bill when the push rule
  leaves it empty. Inheritance failures never fail the push itself — they are
  surfaced as a warning banner in the push dialog.
- The push dialog now passes `source_fid`, renders the inheritance warning,
  blocks double-submits for the same bill while a push is in flight, and offers
  a "重试读取" action when the created bill's details cannot be read back.

### Tests
- Added `tests/test_reimburse_push.py` (8 cases): non-retry on uncertain
  transport, contact inheritance rules, and HTTP endpoint behaviour.

## [1.0.3] - 2026-11-12

### Changed
- Renamed npm package scope from `@local/kingdee-portal` to
  `@littlebeaverstudio/kingdee-portal` so the published package carries a real,
  installable scope instead of the local-only `@local` placeholder.
- Updated the matching plugin id / cordis bundle name and README / PRIVACY
  install examples accordingly. The DSH instance slot id (`kingdee-portal`)
  is unchanged.

## [1.0.2] - 2026-11-09

### Added
- Emoji-styled privacy policy (`PRIVACY.md`) and `SECURITY.md`.
- Public pre-publish scanner (`scripts/check_public.py`).

### Changed
- Stripped all real company names, company codes, domains and client ids from
  comments, fixtures and front-end placeholders.
- `app.py` falls back to `portal_config.example.json` so the repo is clone-and-run.
- Removed the redundant SkillHub installer skill (DSH users install the plugin
  directly).

## [1.0.1] - 2026-11-09

### Changed
- `dsh-plugin/package.json` unprivated; added `repository`, `homepage`,
  `keywords` and `exports`.

## [1.0.0] - 2026-11-06

### Added
- First public release of the local Kingdee Cloud (金蝶云星空) operations
  portal as a DSH plugin: sales / inventory / purchase analytics, AR-AP
  dashboards, invoice and fund ledgers, all served from a local Flask portal
  that opens in the DSH sidebar. Data stays on the user's machine.
