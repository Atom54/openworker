import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  connectManaged,
  disconnectGcalAccount,
  setGcalDefaultAccount,
  type GmailAccount,
} from "../../api";
import { ConnectorBadge } from "../../connectors/ConnectorIcon";
import type { DetailProps } from "./ConnectorsSection";
import { GoogleOneClickSetup } from "./GoogleOneClick";
import { ToolsDisclosure } from "./ToolsDisclosure";
import { FOOT, GRP, GRP_H, PILL_ACCENT, ROW, TAG_ACCENT, TAG_WARN, XBTN } from "./ui";

// The Google Calendar detail page: connected accounts (multi-account, Default
// badge, per-account disconnect) — Gmail's page minus the privacy filters.
// Adding an account launches OAuth DIRECTLY (one connect mode, no modal): local
// with the user's own Google client, the broker's otherwise. Same setup block as
// Gmail's page — one client covers every Google connector.

export function CalendarDetail({ c, cloud, slack: _slack, onChanged }: DetailProps) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const accounts = (c.accounts ?? []) as GmailAccount[]; // email-keyed (pre-generic-layer shape)
  const canAdd = !c.managed_paused && (c.google_client_ready || !!cloud?.signed_in);

  const addAccount = async () => {
    setBusy(true);
    await connectManaged("google_calendar"); // completes in the system browser; the poll picks it up
    setTimeout(() => setBusy(false), 2500);
  };

  return (
    <div data-testid="gcal-detail">
      <div className="flex items-center gap-3.5 mb-5">
        <ConnectorBadge connector={c} size={44} title={t("calendar.google_calendar")} />
        <div className="min-w-0 flex-1">
          <h2 className="text-[20px] font-semibold tracking-tight leading-tight">
            Google Calendar
          </h2>
          <div className="text-[13px] text-muted flex items-center gap-1.5">
            {c.connected ? (
              <>
                <span className="w-2 h-2 rounded-full bg-ok" />
                <span data-testid="gcal-status">
                  {t("connector.account_count", { count: accounts.length })}
                </span>
              </>
            ) : (
              <span>{t("connector.not_connected")}</span>
            )}
          </div>
        </div>
        <button
          className={PILL_ACCENT + (canAdd ? "" : " opacity-50")}
          data-testid="add-account-btn"
          onClick={addAccount}
          disabled={busy || !canAdd}
          title={
            c.managed_paused
              ? t("calendar.setup_below_title")
              : canAdd
                ? ""
                : t("cloud.sign_in_first")
          }
        >
          {c.managed_paused ? t("calendar.add_account_setup_below") : busy ? t("cloud.check_browser") : t("calendar.add_account")}
        </button>
      </div>

      {!c.connected && (
        <div className={GRP}>
          <div className={ROW + " text-[13px] text-muted"}>
            {t("calendar.setup_blurb")}
            {c.google_client_ready || cloud?.signed_in ? "" : " " + t("calendar.setup_oneclick_hint")}
          </div>
        </div>
      )}

      <GoogleOneClickSetup onChanged={onChanged} />

      {accounts.length > 0 && (
        <>
          <div className={GRP_H + " !mt-0"}>{t("calendar.accounts")}</div>
          <div className={GRP} data-testid="gcal-accounts">
            {accounts.map((a) => (
              <AccountRow key={a.email} a={a} onChanged={onChanged} />
            ))}
          </div>
        </>
      )}

      <ToolsDisclosure c={c} onChanged={onChanged} />
      <div className={FOOT + " mt-2"}>
        {t("calendar.events_approval_foot")}
      </div>
    </div>
  );
}

function AccountRow({ a, onChanged }: { a: GmailAccount; onChanged: () => void }) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  return (
    <div className={ROW} data-testid={`gcal-account-${a.email}`}>
      <span className="min-w-0 flex-1 flex items-center gap-2">
        <span className="text-[13px] font-medium truncate">{a.email}</span>
        {a.default && <span className={TAG_ACCENT}>{t("connector.default")}</span>}
        {a.needs_reauth && <span className={TAG_WARN}>{t("calendar.sign_in_again")}</span>}
      </span>
      {!a.default && (
        <button
          className="text-[12px] text-muted hover:text-ink shrink-0"
          data-testid={`gcal-make-default-${a.email}`}
          onClick={async () => {
            await setGcalDefaultAccount(a.email);
            onChanged();
          }}
        >
          {t("connector.make_default")}
        </button>
      )}
      <button
        className={XBTN}
        title={t("calendar.disconnect_account_title")}
        data-testid={`gcal-disconnect-${a.email}`}
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          await disconnectGcalAccount(a.email);
          setBusy(false);
          onChanged();
        }}
      >
        ×
      </button>
    </div>
  );
}
