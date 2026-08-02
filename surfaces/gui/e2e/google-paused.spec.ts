// One-click Google, local edition. The BROKER's one-click stays parked behind Google's
// CASA review (managed_paused), so until the user sets up their own Google Cloud OAuth
// client the button says "needs setup" and offers the setup block right there — with the
// manual token path fully live underneath. Once the client is saved the server sends
// google_client_ready and the same button connects, with NO cloud sign-in involved.
// The shared fixture keeps gmail unpaused (the cloud-machinery specs use its one-click as
// their subject), so this spec overrides the connectors payload per test.
import { expect } from "@playwright/test";
import { test } from "./fixtures";

const GMAIL_BASE = {
  name: "gmail",
  title: "Gmail",
  icon: "✉",
  blurb: "Search, summarize, draft, and send email.",
  about: "Search, summarize, and send over your Gmail.",
  access: ["Reads and searches your mail."],
  auth: "oauth",
  two_way: false,
  channels: false,
  available: true,
  brand_color: "#ea4335",
  logo: "gmail",
  fields: [
    { key: "access_token", label: "OAuth access token", secret: true, required: true, help: "", placeholder: "" },
  ],
  instructions: [],
  account: null,
  allowed_users: [],
  tools: [],
  managed: true,
  managed_paused: true,
  google_client_ready: false,
  managed_profile: false,
};

async function serveGmail(page, extra: Record<string, unknown>) {
  await page.route("**/v1/connectors", (route) =>
    route.fulfill({ json: { connectors: [{ ...GMAIL_BASE, connected: false, enabled: false, ...extra }] } }),
  );
}

async function serveGoogleClient(page, configured: boolean) {
  await page.route("**/v1/google/oauth-client", (route) =>
    route.fulfill({
      json: {
        configured,
        client_id: configured ? "abc.apps.googleusercontent.com" : "",
        from_env: false,
        redirect_uri: "http://127.0.0.1:8765/google/oauth/callback",
      },
    }),
  );
}

async function openConnectors(page) {
  await page.goto("/");
  await page.getByTestId("account-row").click();
  await page.getByRole("button", { name: "Connectors", exact: true }).click();
}

test("no client yet: the connect modal parks one-click and offers the setup fields", async ({
  page,
}) => {
  await serveGoogleClient(page, false);
  await serveGmail(page, {});
  await openConnectors(page);
  await page.getByTestId("connector-gmail").getByRole("button", { name: "Connect", exact: true }).click();

  const soon = page.getByTestId("managed-coming-soon");
  await expect(soon).toBeVisible();
  await expect(soon).toBeDisabled();
  await expect(soon).toContainText("Needs setup");
  // The fix is right there, not "later": paste your own client and it works.
  await expect(page.getByTestId("google-client-setup")).toBeVisible();
  // The manual token field is still right there too.
  await expect(page.getByText("OAuth access token")).toBeVisible();
});

test("no client yet: the connected page's add-account points at the setup below", async ({
  page,
}) => {
  await serveGoogleClient(page, false);
  await serveGmail(page, {
    connected: true,
    enabled: true,
    account: "rohit@gmail.com",
    accounts: [
      { email: "rohit@gmail.com", default: true, managed: true, scopes: "gmail", needs_reauth: false },
    ],
    filters: { senders: [], labels: [] },
  });
  await openConnectors(page);
  await page.getByTestId("connector-gmail").click();
  await expect(page.getByTestId("gmail-detail")).toBeVisible();

  const add = page.getByTestId("add-account-btn");
  await expect(add).toBeDisabled();
  await expect(add).toContainText("Set up below");
  await expect(page.getByTestId("google-client-setup")).toBeVisible();
  // Existing accounts keep working and stay manageable.
  await expect(page.getByTestId("gmail-account-rohit@gmail.com")).toContainText("Default");
});

test("client saved: one-click connects with no cloud sign-in", async ({ page }) => {
  await serveGoogleClient(page, true);
  await serveGmail(page, {
    managed_paused: false,
    google_client_ready: true,
    connected: true,
    enabled: true,
    account: "rohit@gmail.com",
    accounts: [
      { email: "rohit@gmail.com", default: true, managed: false, scopes: "gmail", needs_reauth: false },
    ],
    filters: { senders: [], labels: [] },
  });
  // Signed OUT of the cloud — the local flow must not ask for it.
  await page.route("**/v1/cloud/status", (route) =>
    route.fulfill({ json: { signed_in: false, account: "", user_id: "" } }),
  );
  let started = "";
  await page.route("**/v1/connectors/gmail/connect-managed", (route) => {
    started = route.request().url();
    return route.fulfill({
      json: { ok: true, authorize_url: "https://accounts.google.com/o/oauth2/v2/auth?x=1" },
    });
  });

  await openConnectors(page);
  await page.getByTestId("connector-gmail").click();
  await expect(page.getByTestId("google-client-ready")).toBeVisible();

  const add = page.getByTestId("add-account-btn");
  await expect(add).toBeEnabled();
  await add.click();
  await expect(add).toContainText("Check your browser…");
  expect(started).toContain("/v1/connectors/gmail/connect-managed");
});
