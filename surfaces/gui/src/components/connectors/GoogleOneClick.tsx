import { useEffect, useState } from "react";
import { clearGoogleClient, getGoogleClient, setGoogleClient, type GoogleClientStatus } from "../../api";
import { FOOT, GRP, GRP_H, PILL_ACCENT, PILL_QUIET, ROW } from "./ui";

// Local one-click Google sign-in setup, shared by Gmail, Calendar and Drive.
//
// The broker's one-click is parked behind Google's CASA review, and a broker app
// in "Testing" would only hand out 7-day refresh tokens anyway. Pointing the flow
// at the user's OWN Google Cloud OAuth client fixes both: no review to wait for,
// and the grant keeps refreshing itself for as long as it isn't revoked. Set up
// once here, all three Google connectors light up — signed in to the cloud or not.

const FIELD =
  "w-full bg-paper border border-line rounded-lg px-2.5 py-1.5 text-[12.5px] outline-none focus:border-lineStrong";

export function GoogleOneClickSetup({
  onChanged,
  compact = false,
}: {
  onChanged?: () => void;
  // Inside the add-connection card, where a full instructions block would bury
  // the manual paste path sitting right below it.
  compact?: boolean;
}) {
  const [status, setStatus] = useState<GoogleClientStatus | null>(null);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => getGoogleClient().then(setStatus).catch(() => setStatus(null));
  useEffect(() => {
    load();
  }, []);

  const save = async () => {
    setBusy(true);
    setError(null);
    const res = await setGoogleClient(clientId, clientSecret);
    setBusy(false);
    if (!res.ok) {
      setError(res.error || "could not save the client");
      return;
    }
    setClientId("");
    setClientSecret("");
    setEditing(false);
    await load();
    onChanged?.();
  };

  const forget = async () => {
    setBusy(true);
    await clearGoogleClient();
    setBusy(false);
    await load();
    onChanged?.();
  };

  if (status === null) return null;

  if (status.configured && !editing) {
    return (
      <>
        <div className={GRP_H}>One-click sign-in</div>
        <div className={GRP} data-testid="google-client-ready">
          <div className={ROW}>
            <span className="min-w-0 flex-1">
              <span className="text-[13px] font-medium">Ready</span>
              <span className="block text-[11.5px] text-faint truncate" title={status.client_id}>
                {status.client_id}
              </span>
            </span>
            {status.from_env ? (
              <span className="text-[11.5px] text-faint shrink-0">set by environment</span>
            ) : (
              <>
                <button
                  className="text-[12px] text-muted hover:text-ink shrink-0"
                  onClick={() => setEditing(true)}
                >
                  Change
                </button>
                <button
                  className="text-[12px] text-muted hover:text-danger shrink-0"
                  data-testid="google-client-forget"
                  disabled={busy}
                  onClick={forget}
                >
                  Forget
                </button>
              </>
            )}
          </div>
        </div>
        <div className={FOOT}>
          Accounts you add stay signed in — the token renews itself. Google still ends a
          connection if you revoke access, change your password, or leave it unused for six
          months.
        </div>
      </>
    );
  }

  return (
    <>
      <div className={GRP_H}>Set up one-click sign-in</div>
      <div className={GRP} data-testid="google-client-setup">
        {!compact && (
          <ol className="list-decimal pl-8 pr-4 py-3 text-[12.5px] text-muted leading-relaxed space-y-1">
            <li>
              Open{" "}
              <a
                className="text-accent hover:underline"
                href="https://console.cloud.google.com/auth/clients"
                target="_blank"
                rel="noreferrer"
              >
                Google Cloud Console
              </a>{" "}
              and create a <b>new</b> project — a private one, just to hold this client. Don't
              reuse a project whose consent screen already serves your own users: adding mail
              scopes there would put it back through Google's review.
            </li>
            <li>
              Enable the APIs you want under <b>APIs &amp; Services → Library</b>: Gmail API,
              Google Calendar API, Google Drive API.
            </li>
            <li>
              On the <b>Audience</b> screen pick <b>External</b>, then <b>Publish app</b> — that
              part matters: an app left in <b>Testing</b> makes you sign in again every 7 days,
              a published one stays connected. No verification to submit; you'll see a "Google
              hasn't verified this app" screen once and click <b>Advanced → Continue</b>. (A
              Workspace account can pick <b>Internal</b> instead — nothing to publish, no warning
              screen, but then only accounts in that Workspace can connect: a personal
              @gmail.com mailbox would need a second, External project.)
            </li>
            <li>
              Create an OAuth client, application type <b>Desktop app</b>. No redirect URI to
              fill in — desktop clients accept this app's local callback on any port.
            </li>
            <li>Paste the client ID and client secret below.</li>
          </ol>
        )}
        <div className="px-4 py-3 space-y-2">
          <input
            className={FIELD}
            data-testid="google-client-id"
            placeholder="123…-abc.apps.googleusercontent.com"
            spellCheck={false}
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
          />
          <input
            className={FIELD}
            data-testid="google-client-secret"
            type="password"
            placeholder="Client secret (GOCSPX-…)"
            spellCheck={false}
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
          />
          <div className="flex items-center gap-2">
            <button
              className={PILL_ACCENT}
              data-testid="google-client-save"
              disabled={busy}
              onClick={save}
            >
              {busy ? "Saving…" : "Enable one-click"}
            </button>
            {editing && (
              <button className={PILL_QUIET} onClick={() => setEditing(false)}>
                Cancel
              </button>
            )}
          </div>
          {error && <div className="text-[12.5px] text-danger">{error}</div>}
        </div>
      </div>
      <div className={FOOT}>
        The client stays on this computer, and covers Gmail, Calendar and Drive at once. Sign-ins
        made with it don't expire — you connect an account once and it keeps working.
      </div>
    </>
  );
}
