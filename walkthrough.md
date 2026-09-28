# Walkthrough — Premium Dark Dashboard for Dhyaan Voice Brain

We have completed the implementation of the stunning, premium, dark glassmorphic single-page dashboard for the Dhyaan Voice Brain AI calling system (Session 1).

The application is built inside the repository at:
`<project-folder>/dhyaan-voice-brain`

---

## 1. Accomplished Features

- **FastAPI Backend (`dashboard/app.py`)**: 
  - Starts a server on port `5006`.
  - Connects to the `voicebrain` PostgreSQL schema based on the local `.env` configuration.
  - Queries active metrics (total leads, called leads, verified leads, callbacks count, monthly cost spend) and pulls the complete list of leads to render.
  - Exposes a `/upload` route that clean-normalizes raw CSV leads in dry-run mode via `intake.clean_leads.process()` and inserts them live using `intake.live_intake.run()` under `funnel_stage = 'whatsapp'`.

- **Fintech-Aesthetic Front-End (`dashboard/templates/index.html`)**:
  - **Color Palette**: Deep navy `#0A1628` background with an animated top-left gradient navy glow `#13243f` for visual depth. Gold `#D4AF37` primary buttons, highlights, and nav accents. Mint `#5DCAA5` (verified leads, whatsapp column), blue `#378ADD` (AI calling column), amber `#EF9F27` (callbacks), and slate `#6b7d99` (closed/muted text).
  - **Frosted Glass Cards**: Sleek panels built with CSS `backdrop-filter: blur(12px)` and transparent white borders, reflecting a modern fintech theme. Soft hover effects lift cards by 2px and brighten borders.
  - **Hero Row**:
    1. *Verified Leads*: Displays count, today's increment, and a smooth gold-glowing Chart.js trend line.
    2. *Monthly Spend*: Large currency number representing month-to-date spend (fallback to ₹0 if empty).
  - **Metric Tiles**: Four cards displaying total leads, called count, verified count, and callbacks with count-up animations on page load.
  - **Funnel Board (Kanban)**: Four vertical columns mapping WhatsApp, AI Calling (with blue pulse animation), Human, and Closed stages. Displays lead names and masked phones (`+91 ····· last4`).
  - **In-Flow Upload Modal**: Clean upload window sitting within the main content space, supporting drag-and-drop file imports, campaign naming, and consent stamping.
  - **Auto-Refresh**: Simple 15-second automatic page reload (automatically paused if the upload modal is open).

---

## 2. Technical Validation

### Database Schema Verification
- A database schema checker was executed to confirm column naming matching:
  - `voicebrain.call_leads`: `attempts`, `call_status`, `funnel_stage`, `whatsapp_replied`, `outcome`.
  - `voicebrain.call_results`: `call_outcome`, `cost`, `received_at`.
- All dashboard SQL queries were aligned perfectly with these column names.

### Intake Integration Verification
- The `/upload` endpoint performs direct programmatic calls:
  - `intake.clean_leads.process(file_path=file_path, campaign=campaign_name, dry_run=True, consent_source=consent_source)`
  - `intake.live_intake.run(file_path=processed_batch_path, campaign=campaign_name, consent_source=consent_source, dry_run=False)`
- Confirmed that no intake modules were forked or duplicated.

### Compiler Pass
- Verified syntax via `python -m py_compile dashboard/app.py`, which completed with exit code 0.

### Server Boot
- Uvicorn runs cleanly on:
  **`http://localhost:5006`**

---

## 3. Visual Layout Reference
The sidebar on the left displays the logo and active Home icon with a gold marker bar. The main content is split into a top bar with action buttons, a hero row featuring the glowing gold sparkline, a row of metric tiles, and the four-column kanban board populated by lead cards. The dark, navy, and gold colors match a premium trading dashboard.
