/* Settings → Inboxes → "Add an inbox with email + password": fills server settings from the
   address (autoconfig) and tests the login before saving. */
(() => {
  "use strict";
  const form = document.querySelector("[data-smtp-form]");
  if (!form) return;

  const email = form.querySelector("#smtp_email");
  const status = form.querySelector("[data-autoconfig-status]");
  const servers = form.querySelector("[data-servers]");
  const oauthNote = form.querySelector("[data-oauth-note]");
  const steps = form.querySelector("[data-steps]");
  const testButton = form.querySelector("[data-smtp-test]");
  const set = (name, value) => { if (value !== undefined && value !== null) form.elements[name].value = value; };
  let looked = "";

  async function lookup() {
    const address = email.value.trim().toLowerCase();
    const domain = address.split("@")[1] || "";
    if (!domain.includes(".") || domain === looked) return;
    looked = domain;
    status.textContent = "Looking up server settings…";
    try {
      const response = await fetch(`${email.dataset.autoconfigUrl}?email=${encodeURIComponent(address)}`);
      const found = await response.json();
      if (!found.found) {
        status.textContent = "Couldn't find settings for this domain. Enter them below.";
        servers.open = true;
        return;
      }
      set("smtp_host", found.smtp_host); set("smtp_port", found.smtp_port); set("smtp_security", found.smtp_security);
      set("imap_host", found.imap_host); set("imap_port", found.imap_port); set("imap_security", found.imap_security);
      if (found.username && found.username !== address) set("username", found.username);
      status.textContent = `Found ${found.provider} settings: ${found.smtp_host}.`;
      oauthNote.hidden = !found.oauth_preferred;
      form.querySelector("[data-oauth-text]").textContent =
        `${found.provider} prefers app sign-in (OAuth). Password login may be turned off; use an app password if your account offers one.`;
    } catch {
      status.textContent = "Lookup failed. Enter the settings below.";
      servers.open = true;
    }
  }
  email.addEventListener("change", lookup);
  email.addEventListener("blur", lookup);

  // "Reconnect" on a paused SMTP inbox: open the form for that address.
  document.querySelectorAll("[data-smtp-reconnect]").forEach((link) =>
    link.addEventListener("click", (event) => {
      event.preventDefault();
      form.closest("details").open = true;
      email.value = link.dataset.smtpReconnect;
      lookup();
      form.elements.password.focus();
    }),
  );

  function show(results) {
    steps.replaceChildren(
      ...results.map((s) => {
        const li = document.createElement("li");
        li.className = s.ok === true ? "ok" : s.ok === false ? "bad" : "note";
        li.textContent = `${s.ok === true ? "✓" : s.ok === false ? "✗" : "·"} ${s.step}${s.detail ? ": " + s.detail : ""}`;
        return li;
      }),
    );
    steps.hidden = false;
  }

  testButton.addEventListener("click", async () => {
    if (!form.reportValidity()) return;
    testButton.disabled = true;
    testButton.textContent = "Testing…";
    try {
      const response = await fetch(testButton.dataset.url, { method: "POST", body: new FormData(form) });
      show((await response.json()).steps);
    } catch {
      show([{ step: "Test", ok: false, detail: "No response from the server." }]);
    } finally {
      testButton.disabled = false;
      testButton.textContent = "Test connection";
    }
  });
})();
