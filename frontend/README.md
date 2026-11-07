# IoT Lab Inventory Management System — Frontend Guide

> **For the frontend developer**  
> Status: Frontend development can begin now. Backend APIs and authentication contracts must be verified before integration.  
> Target: A reliable, accessible, responsive application for faculty, lab assistants, and students.

## 1. Project overview

This application manages an IoT laboratory's equipment inventory (approximately 100–500 items), checkouts/returns, maintenance, audits, and related reporting. Faculty register equipment to students; students **cannot issue equipment to themselves**. Certain items may leave the lab, while others must stay inside. Borrowing deadlines are configurable, and overdue reminders are planned.

The software must be suitable for real institutional use, not just a demonstration. Prioritize clarity, safe actions, permission-aware screens, and good error handling over decorative animations.

### User roles

| Role | Expected experience | Important limitation |
|---|---|---|
| Faculty | Inventory, checkouts/returns, maintenance, audits, reporting, authorized administrative actions | Actual permissions must follow backend policy |
| Lab assistant | Operational inventory, issue/return, audits, maintenance as authorized | Do not assume identical permissions to faculty |
| Student | View own borrowing records and permitted equipment information | Cannot register equipment or view other students' borrowing history |

**Security rule:** Hiding buttons in the UI is not authorization. The backend must enforce permissions for every operation. Never trust a role value stored only in the browser.

## 2. Recommended stack

- React + TypeScript + Vite
- React Router for routing
- TanStack Query for API state/caching
- React Hook Form + Zod for forms and validation
- Tailwind CSS for styling (optional: use a consistent accessible component library)
- Vitest + React Testing Library for unit/component tests
- Playwright for critical end-to-end flows

Use the versions approved by the team; do not commit dependencies that have not been installed and tested.

## 3. Repository layout

The existing repository has a `backend/` and `frontend/` directory. **Keep frontend work inside `frontend/`.** Do not rename or restructure the backend.

```text
Computer_Networks/
├── backend/                      # FastAPI backend — backend developer owns this
├── frontend/
│   ├── public/
│   ├── src/
│   │   ├── app/
│   │   │   ├── App.tsx
│   │   │   ├── router.tsx
│   │   │   └── providers.tsx
│   │   ├── assets/
│   │   ├── components/
│   │   │   ├── ui/                # Buttons, inputs, dialogs, badges, tables
│   │   │   ├── layout/            # Sidebar, header, page shell
│   │   │   └── feedback/          # Loading, empty, error, confirmation states
│   │   ├── features/
│   │   │   ├── auth/
│   │   │   ├── dashboard/
│   │   │   ├── equipment/
│   │   │   ├── borrowing/
│   │   │   ├── maintenance/
│   │   │   ├── audits/
│   │   │   ├── users/
│   │   │   └── reports/
│   │   ├── lib/
│   │   │   ├── api/               # Shared HTTP client, errors, types
│   │   │   └── utils/
│   │   ├── mocks/                 # Clearly labeled development-only data
│   │   ├── styles/
│   │   ├── test/
│   │   └── main.tsx
│   ├── tests/                     # End-to-end tests
│   ├── .env.example
│   ├── .gitignore
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   └── README.md
├── .gitignore
└── README.md
```

Organize each feature by its own pages, components, hooks, API calls, and types when needed. Avoid putting the entire application in `App.tsx`.

## 4. Pages and deliverables

| Phase | Page/feature | Frontend requirements | Integration dependency |
|---|---|---|---|
| 1 | App shell | Responsive sidebar/topbar, breadcrumbs, role-aware navigation, 404 page | None; mock roles temporarily |
| 1 | Equipment list | Search, filters, sorting, pagination, status badges, empty/loading/error states | Equipment list API |
| 1 | Equipment details | Metadata, individually tracked assets or bulk stock, condition, location, activity placeholders | Equipment detail/asset APIs |
| 1 | Dashboard | KPI cards, due/overdue summary, maintenance alerts, recent activity | Dashboard/aggregate APIs or agreed client-side aggregation |
| 2 | Equipment registration | Validated create/edit forms, individual/bulk workflows, success/error feedback | Create/update and bulk APIs |
| 2 | Borrowing | Faculty/assistant issue and return flows, due dates, borrower lookup, policy warnings | Checkout/return APIs |
| 2 | My borrowings | Student-only own history, current loans, due dates | Current-user borrowing API |
| 2 | Maintenance | Request, status, notes, repair history, return-to-service UI | Maintenance APIs |
| 3 | Audits | Scan entry, progress, matched/unmatched results, discrepancy review | Audit APIs + scanner contract |
| 3 | Users | Authorized user search/management | Identity/user management APIs |
| 3 | Reports | Filtered tables, export UI only when supported | Reporting/export APIs |
| 3 | Notifications | Due/overdue indicators and status | Notification APIs or agreed behavior |

**Do not present planned features as implemented.** A mock page is fine if it is explicitly marked as a prototype. Avoid inventing API URLs, request fields, or response schemas.

## 5. Example visual direction

Build a clean **institutional operations dashboard**:

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ IoT LAB INVENTORY                    Search…       Alerts   User ▾      │
├─────────────────┬───────────────────────────────────────────────────────┤
│ Dashboard       │ Overview                                              │
│ Equipment       │                                                       │
│ Borrowing       │ [Total items] [Available] [Checked out] [Needs care]  │
│ Maintenance     │                                                       │
│ Audits          │ Recent activity                Due soon               │
│ Reports         │ ┌──────────────────────────┐   ┌───────────────────┐ │
│ Users*          │ │ Arduino Uno  • Returned  │   │ ESP32 • Tomorrow  │ │
│                 │ │ Sensor Kit   • Issued    │   │ Kit   • Overdue   │ │
│                 │ └──────────────────────────┘   └───────────────────┘ │
└─────────────────┴───────────────────────────────────────────────────────┘
*Only shown when authorized.
```

**Design principles:** neutral background, restrained accent color, high contrast, readable tables, consistent spacing, clear statuses, keyboard navigation, mobile-friendly layouts, and confirmation dialogs for consequential actions. Never rely on color alone to communicate status.

### Example equipment table

| Equipment | Category | Tracking | Availability | Location | Action |
|---|---|---|---|---|---|
| Arduino Uno R3 | Microcontrollers | Individual | Available | Shelf A | View |
| ESP32 Board | Microcontrollers | Individual | Checked out | — | View |
| Jumper Wires | Accessories | Bulk | 24 available | Drawer B | View |

*These are illustrative mock records, not live inventory data.*

## 6. Backend integration contract — verify first

The backend uses **Python/FastAPI, PostgreSQL, SQLAlchemy, Alembic, and Pydantic**. When the backend is running locally, its interactive API documentation is typically at:

```text
http://127.0.0.1:8000/docs
http://127.0.0.1:8000/openapi.json
```

The URL above is **local to the machine running the backend**, not a shared team server. The frontend developer must not assume it works on their computer. Agree on one of: a local backend instance with test data, an approved development server, or a temporary mock API that matches a verified OpenAPI schema.

Before wiring each feature, agree on:

1. Exact HTTP method and endpoint path.
2. Request and response types, including pagination and error payloads.
3. Authentication method (college SSO is planned, subject to college IT approval).
4. Authorization rules for faculty, lab assistants, and students.
5. Status enums, date/time formats, and validation constraints.
6. Whether a feature is available, under development, or only planned.

### Example environment configuration

Create `frontend/.env.example`:

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000
```

Use this only as a local default. Vite `VITE_` values are exposed to browser code: **never put passwords, API secrets, database credentials, or private tokens in them**. Do not commit `.env.local`.

### Example API client (illustrative)

```ts
// src/lib/api/client.ts
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include", // ONLY if backend chooses cookie-based sessions
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}
```

**This is a pattern, not an agreed authentication implementation.** Replace it with the team's verified auth/CORS/CSRF strategy; do not assume cookies, bearer tokens, or endpoint paths without backend confirmation. Do not put sensitive access tokens in `localStorage` by default.

## 7. Development workflow

### Initial setup

From the repository root:

```powershell
cd frontend
npm create vite@latest . -- --template react-ts
npm install
npm run dev
```

Run the Vite scaffolding command **only if `frontend/` does not already contain an app**; inspect existing files first and avoid overwriting teammates' work. Add the selected dependencies as needed and commit the lockfile.

### Collaboration rules

- Use a dedicated frontend branch (for example `feature/frontend-foundation`) and open pull requests.
- Do not modify backend files without coordinating with the backend owner.
- Keep UI mock data isolated in `src/mocks/`; never silently fall back to fake data in production.
- Report missing/unclear APIs with the required screen, sample payload, and expected behavior.
- Never bypass authentication or authorization to make a demo work.
- Merge small, reviewed changes; avoid large unreviewed UI dumps.

## 8. Required UX states and safety checks

Every data-driven page must support **loading**, **empty**, **success**, **validation error**, **network/server error**, and **permission denied** states where applicable.

For mutations (register, issue, return, maintenance updates): disable duplicate submissions, show actionable errors, and refresh affected data after success. For destructive or irreversible actions, ask for confirmation. Display deadlines in the user's local time zone while respecting the backend's date/time semantics. Make search and filters usable without requiring a mouse.

For offline scenarios, **do not implement automatic offline checkout/return** until faculty authorization, conflict handling, local data protection, and reconciliation rules are explicitly defined with the backend team. A read-only offline indicator is safer as a first iteration.

## 9. Definition of done

A feature is ready for review when:

- [ ] It matches the agreed design and works at desktop and mobile widths.
- [ ] It uses verified API schemas or clearly labeled mock data.
- [ ] It handles loading, empty, error, and access-denied states.
- [ ] Forms validate inputs and show understandable errors.
- [ ] Role-aware navigation is correct; backend permissions remain authoritative.
- [ ] TypeScript, linting, and relevant tests pass.
- [ ] No secrets, hardcoded production credentials, or student personal data are committed.
- [ ] The pull request includes screenshots, test steps, and any backend dependencies.

## 10. Suggested first milestone (start here)

**Milestone 1: Navigable frontend prototype**

1. Initialize the React/TypeScript app without touching backend code.
2. Create the sidebar, header, routing, theme, and reusable UI components.
3. Build the Equipment List and Equipment Details screens with clearly labeled mock data.
4. Build a dashboard with sample KPI cards and recent activity.
5. Add responsive, loading, empty, and error states.
6. Share screenshots and a pull request with the backend developer for review.
7. Inspect the actual FastAPI OpenAPI schema and integrate only verified endpoints.

**Expected deliverable:** a polished, navigable UI prototype, with a clear list of endpoints needed next. This can proceed independently while the backend developer completes API and security work.

---

### Questions for the backend developer

- Which endpoints are stable today, and can we share the current OpenAPI schema?
- What are the exact equipment/asset/stock types and status values?
- What authentication and session mechanism will be used?
- What are the final permission differences between faculty and lab assistants?
- How will barcode/NFC scanning data reach the web app?
- Which reporting, audit, borrowing, and maintenance endpoints are ready for integration?

**Ownership:** Frontend developer owns UI, UX, client-side state, and frontend tests. Backend developer owns database, API behavior, permissions, authentication enforcement, and backend tests. Coordinate on contracts; do not duplicate one another's work.
