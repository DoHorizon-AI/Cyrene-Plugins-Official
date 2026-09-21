// -----------------------------------------------------------------------------
// Module: src/App.tsx
// Role: Session gate, shell navigation, and page composition for Navigator UI.
// -----------------------------------------------------------------------------

import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import { Button, formatDate, StateBlock } from "./components";
import { NavigatorApi, NavigatorHttpError, type SessionPayload } from "./api";
import {
  DatasetsPage,
  DeploymentsPage,
  GatewayPage,
  ModelsPage,
  OverviewPage,
  RunsPage,
  SettingsPage,
  TrainingPage,
} from "./pages";
import { pushRoute, routeForPath, ROUTES, type RouteId } from "./router";

/** Own the browser session gate and mount only authenticated Product surfaces. */
export function App() {
  const [api] = useState(() => new NavigatorApi());
  const [session, setSession] = useState<SessionPayload | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);

  useEffect(() => {
    api.setSessionExpiredHandler(() => {
      setSession(anonymousSession());
    });
    return () => api.setSessionExpiredHandler(null);
  }, [api]);

  useEffect(() => {
    let active = true;
    void api.restoreSession().then(
      (value) => {
        if (active) {
          setSession(value);
          setBootError(null);
        }
      },
      (reason: unknown) => {
        if (active) {
          setBootError(errorMessage(reason));
        }
      },
    );
    return () => {
      active = false;
    };
  }, [api]);

  useEffect(() => {
    if (!session?.authenticated || !session.expiresAt) {
      return;
    }
    const expiresAt = new Date(session.expiresAt).getTime();
    const delay = Math.max(1_000, expiresAt - Date.now() - 30_000);
    const timer = window.setTimeout(() => {
      void api.refreshSession().then(
        (value) => setSession(value),
        (reason: unknown) => {
          if (reason instanceof NavigatorHttpError && reason.status === 401) {
            setSession(anonymousSession());
          }
        },
      );
    }, delay);
    return () => window.clearTimeout(timer);
  }, [api, session]);

  if (bootError) {
    return (
      <AppFrame>
        <div className="center-state">
          <StateBlock
            kind="error"
            title="Navigator Web Host is unreachable"
            detail={bootError}
            action={<Button onClick={() => window.location.reload()}>Retry session check</Button>}
          />
        </div>
      </AppFrame>
    );
  }

  if (!session) {
    return (
      <AppFrame>
        <div className="center-state">
          <StateBlock kind="loading" title="Opening Navigator" detail="Checking the same-origin Web Host session." />
        </div>
      </AppFrame>
    );
  }

  if (!session.authenticated) {
    return <LoginView api={api} onAuthenticated={setSession} />;
  }

  return (
    <AppShell
      api={api}
      session={session}
      onSessionChange={setSession}
    />
  );
}

interface LoginViewProps {
  api: NavigatorApi;
  onAuthenticated: (session: SessionPayload) => void;
}

/** Pair with the launcher-delivered one-time code without persisting it. */
function LoginView({ api, onAuthenticated }: LoginViewProps) {
  const [pairingCode, setPairingCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      onAuthenticated(await api.pair(pairingCode));
    } catch (pairError) {
      setError(errorMessage(pairError));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AppFrame>
      <main className="login-layout">
        <section className="login-card">
          <div className="brand-mark brand-mark--large" aria-hidden="true">N</div>
          <p className="eyebrow">Navigator / Web Host</p>
          <h1>Enter the field.</h1>
          <p className="login-copy">Pair this browser with the local Navigator host. The code is printed once by the host launcher and is never stored by the UI.</p>
          <form onSubmit={submit}>
            <label className="field">
              <span className="field__label">One-time pairing code</span>
              <input
                autoFocus
                className="input-mono input-large"
                value={pairingCode}
                onChange={(event) => setPairingCode(event.target.value)}
                autoComplete="one-time-code"
                placeholder="Paste the launcher code"
              />
            </label>
            <Button tone="primary" type="submit" disabled={submitting || !pairingCode.trim()}>
              {submitting ? "Pairing..." : "Open console"}
            </Button>
            {error ? <p className="form-message form-message--error" role="alert">{error}</p> : null}
          </form>
          <p className="login-footnote">Same-origin session | rotating refresh | CSRF protected mutations</p>
        </section>
        <aside className="login-aside" aria-label="Navigator boundary notes">
          <span className="eyebrow">What stays true</span>
          <strong>Every page reads the owner.</strong>
          <p>Navigator presents Product projections. It does not copy lifecycle state into browser storage or choose arbitrary upstream origins.</p>
          <div className="login-aside__line" />
          <span className="mono-label">SESSION / PRODUCT / PROXY</span>
        </aside>
      </main>
    </AppFrame>
  );
}

interface AppShellProps {
  api: NavigatorApi;
  session: SessionPayload;
  onSessionChange: (session: SessionPayload) => void;
}

/** Desktop rail plus responsive page frame for the seven route surfaces. */
function AppShell({ api, session, onSessionChange }: AppShellProps) {
  const [route, setRoute] = useState<RouteId>(() => routeForPath(window.location.pathname));

  useEffect(() => {
    const handlePopState = () => setRoute(routeForPath(window.location.pathname));
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigate = (nextRoute: RouteId) => {
    if (nextRoute !== route) {
      pushRoute(nextRoute);
    }
  };

  const logout = async () => {
    try {
      await api.logout();
    } finally {
      onSessionChange(anonymousSession());
    }
  };

  const currentRoute = ROUTES.find((definition) => definition.id === route) ?? ROUTES[0];

  return (
    <div className="console-shell">
      <aside className="side-rail">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">N</div>
          <div>
            <strong>Navigator</strong>
            <span>operations console</span>
          </div>
        </div>
        <div className="rail-rule" />
        <nav className="main-nav" aria-label="Primary navigation">
          {ROUTES.map((definition) => (
            <button
              className={`nav-item ${definition.id === route ? "nav-item--active" : ""}`.trim()}
              key={definition.id}
              onClick={() => navigate(definition.id)}
              aria-current={definition.id === route ? "page" : undefined}
            >
              <span className="nav-item__glyph" aria-hidden="true">{navGlyph(definition.id)}</span>
              <span>
                <strong>{definition.label}</strong>
                <small>{definition.description}</small>
              </span>
            </button>
          ))}
        </nav>
        <div className="rail-footer">
          <span className="status-led" aria-hidden="true" />
          <span>Web Host session active</span>
        </div>
      </aside>

      <main className="main-column">
        <header className="topbar">
          <div className="topbar__context">
            <span className="topbar__path">Navigator / {currentRoute.label}</span>
            <span className="topbar__mode"><span className="status-led" aria-hidden="true" /> SAME-ORIGIN</span>
          </div>
          <div className="topbar__session">
            <span className="topbar__expiry">Refresh window | {formatDate(session.refreshExpiresAt)}</span>
            <Button onClick={() => void logout()}>Sign out</Button>
          </div>
        </header>
        <div className="mobile-nav" aria-label="Mobile navigation">
          {ROUTES.map((definition) => (
            <button className={definition.id === route ? "mobile-nav__item mobile-nav__item--active" : "mobile-nav__item"} key={definition.id} onClick={() => navigate(definition.id)}>
              {definition.label}
            </button>
          ))}
        </div>
        <div className="page-container">{renderPage(route, api, session)}</div>
      </main>
    </div>
  );
}

function renderPage(route: RouteId, api: NavigatorApi, session: SessionPayload): ReactNode {
  switch (route) {
    case "models":
      return <ModelsPage api={api} />;
    case "datasets":
      return <DatasetsPage api={api} />;
    case "training":
      return <TrainingPage api={api} />;
    case "runs":
      return <RunsPage api={api} />;
    case "deployments":
      return <DeploymentsPage api={api} />;
    case "gateway":
      return <GatewayPage api={api} />;
    case "settings":
      return <SettingsPage api={api} session={session} />;
    case "overview":
    default:
      return <OverviewPage api={api} />;
  }
}

function navGlyph(route: RouteId): string {
  return {
    overview: "OV",
    models: "MD",
    datasets: "DS",
    training: "TR",
    runs: "RN",
    deployments: "DP",
    gateway: "GW",
    settings: "ST",
  }[route];
}

function anonymousSession(): SessionPayload {
  return {
    authenticated: false,
    state: "ANONYMOUS",
    sessionId: null,
    expiresAt: null,
    refreshExpiresAt: null,
    refreshable: false,
    csrfToken: null,
    refreshed: false,
  };
}

function errorMessage(error: unknown): string {
  if (error instanceof NavigatorHttpError) {
    return `${error.detail} (${error.code})`;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "The Navigator Web Host did not return a readable error.";
}

function AppFrame({ children }: { children: ReactNode }) {
  return <div className="app-root">{children}</div>;
}
