# Changelog

All notable changes to the Kingdee Portal DSH plugin are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [1.2.1] - 2026-11-27

### Fixed
- 核定金额写回复核不再调用 ExecuteBillQuery：该租户元数据无 `FEntity_FCheckedOrgAmount` 字段，
  查询抛 MsgCode=9（且 vendor 客户端抛 `SystemExit`，旧 `except Exception` 兜不住）导致审批报
  「金蝶接口报错（MsgCode=9）」而中止。改为用 Save 的 `NeedReturnFields` 在同一次响应里回读。
- 回读值不一致仍中止审批；金蝶未回传核定字段时不再硬卡，审批继续并在完成弹窗告警「请在金蝶核对」。

## [1.2.0] - 2026-11-27

### Fixed
- Checked-amount verification now reads through ExecuteBillQuery first, falling
  back to View.审核中单据 View 常不回传核定金额（恒为 0），旧逻辑误判写回失败。
  错误信息同时显示查询值与 View 值，便于定位。
- Dark theme: text inputs no longer turn white on focus; browser autofill on
  the login form is forced to the theme background. Affects login and all forms.

## [1.1.9] - 2026-11-27

### Fixed
- Checked-amount write-back: pass FEntryID as integer (string IDs make Kingdee
  report success without writing to the entry) and disable IsAutoAdjustField
  so the amount is not recalculated back to the original value.
- Verification failure now reports expected vs. actual amount and entry ID.

## [1.1.8] - 2026-11-27

### Changed
- Approval checked amount is now the last column of the expense detail table
  (labeled 「审核核定金额」), defaulting to the current checked amount and editable
  there; the separate full-width amount block was removed.
- Detail rows are now built strictly by row index, so sparse fields and skipped
  empty rows no longer shift column values or checked amounts onto the wrong entry.

## [1.1.7] - 2026-11-27

### Fixed
- Base-data names embedded in the value itself (`{"Number":..,"Name":[{"Key":2052,..}]}`)
  are now read directly, so expense item / project no longer show codes on travel
  and expense applications. Multilingual FName from lookup queries is decoded too.
- Modal maximize button now applies inline sizing so the `+` always works even
  after the dialog was resized.

### Added
- Both approval lists show amount and reason/remark; missing applicant/amount on
  completed approvals is backfilled by bill-ID queries.
- Approving an expense/travel application can write back adjusted checked amounts.
  Save is verified before the audit call; any mismatch aborts the approval.

## [1.1.6] - 2026-11-27

### Fixed
- Code-to-name lookup retries field-by-field after a batch name query fails, so
  one invalid customized field no longer hides names for the whole detail grid
  (travel application expense item).
- Payment application header now shows 申请付款金额 and reads `FAPPLYAMOUNTFOR_H`;
  the always-zero 应付金额 column is no longer shown.
- Travel application organization reads `FOrgID` / `ApplyOrgID` as well as `OrgId`,
  so the approver dialog no longer shows an empty organization.

### Added
- All modal dialogs can be resized by dragging the corner; a + button next to
  the close icon toggles an enlarged view.

## [1.1.5] - 2026-11-27

### Fixed
- Base-data code resolution now looks up alphanumeric codes (e.g. `FY001`) by
  `FNumber` instead of by primary key. This fixes expense-project, project and
  cost-department columns still showing codes on expense and travel applications.
- Source bill type now displays its Chinese document name (e.g. `AP_Payable`
  shows as 应付单) instead of the raw FormId; source row ID is hidden.
- Departure/arrival (费控) columns are removed from expense detail grids.

## [1.1.4] - 2026-11-27

### Fixed
- Approval detail grids resolve base-data codes (expense item, project, cost
  department, departure/arrival, etc.) to Chinese names instead of showing raw
  IDs/numbers. Resolution tries sibling name fields, the bill's own `FName`
  query, then common base-data forms; applies to expense bills and any generic
  approval document.
- Amount and numeric detail columns are rendered with thousands separators and
  two decimals (amounts with ¥) instead of raw text.
- Dynamic detail cells no longer wrap; horizontal scrolling keeps row heights
  stable when many columns are present.

## [1.1.3] - 2026-11-27

### Added
- Approval detail grids now detect columns per bill type and show only fields
  that actually contain a value. Travel applications and travel reimbursements
  use the tenant's Kingdee field dictionary for Chinese labels (dates, project,
  fares, transport, lodging, subsidy, special note, cost department, etc.).
- Field dictionaries for the four expense forms plus payment application/bill
  are bundled in `docs/field-docs/` so labels work without a companion skill.

### Changed
- Generic approval details can now show up to 30 detail columns instead of a
  fixed eight, using the same field dictionary for customized keys. Empty
  placeholder columns on payment forms were removed; every column must have a value.
- Reimbursement detail reading accepts both `ER_ExpenseReimbEntry` and `FEntity`
  subtable keys used by different Kingdee versions.

## [1.1.2] - 2026-11-23

### Fixed
- "My approvals" no longer filters pending tasks by a hard-coded list of four
  expense forms, which hid payment applications (`CN_PAYAPPLY`), payment bills,
  purchase orders and every other workflow-enabled bill. The pending query now
  scopes only by reviewer and status, so anything routed to the signed-in user
  is listed and can be approved or rejected through the same `WorkflowAudit` call.
- Forms without a tailored mapping fall back to a generic detail reader (a
  dynamic `View` call) showing the header fields and the first detail grid;
  tasks without a bill id stay visible but read-only instead of failing the
  whole list.
- Completed-approval history accepts any form identifier and reads the current
  bill status through a three-field query for forms that have no tailored map.

### Changed
- Approval detail rendering splits into expense (tailored) and generic modes.
  The history form filter adds payment application, payment bill, purchase
  order and sales order.

## [1.1.1] - 2026-11-18

### Added
- Ported public-edition v1.6.5 approval history: entry renamed to "我的审批"
  with pending/completed tabs. New `workflow_history.py` reads the signed-in
  user's completed tasks from both `WF_AssignmentHisBill` and
  `WF_AssignmentBillEndHis`, deduplicated by task ID; new GET
  `/api/workflow/history` and `/api/workflow/history/<aid>` with filtering and
  paging. History detail is read-only and keeps Kingdee's original decision
  text; a personal approval does not claim the whole bill is approved.

## [1.1.0] - 2026-11-18

### Added
- Ported four feature batches from the public edition (v1.6.1–v1.6.4), keeping
  all DSH local-mode customizations: reimbursement payment rules
  (`reimburse_payment.py`), request close via official `BillClose`, "My approvals"
  (`workflow_actions.py` + `/api/workflow/*`), and paginated bill lists with a
  compact filter bar (`bill_lists.py` + `list_filters.py`).
- Added tests: `test_reimburse_payment.py`, `test_request_close.py`,
  `test_workflow.py`, `test_bill_lists.py`, updated `test_reimburse_push.py`.

### Changed
- License is now LittleBeaverStudio No-Resale License 1.0 (personal and internal
  business use free, no resale).

## [1.0.4] - 2026-11-17

### Fixed
- Prevented automatic retries of reimbursement push requests and preserved the
  created bill ID when later reads or contact inheritance fail.
- Added clear recovery guidance for uncertain results and inherited the contact
  unit from the verified source request when needed.

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
