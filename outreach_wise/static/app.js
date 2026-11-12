/* Shared UI helpers. In-page confirmation instead of window.confirm(), which some embedded
   browsers block (it returns false immediately, so the action silently never happens). */
(() => {
  "use strict";

  let dialog = null;
  function build() {
    dialog = document.createElement("dialog");
    dialog.className = "modal confirm";
    dialog.innerHTML = `
      <form method="dialog">
        <div class="modal-body"><p class="confirm-text"></p></div>
        <div class="modal-foot">
          <button class="btn ghost" value="no">Cancel</button>
          <button class="btn primary" value="yes" data-ok>OK</button>
        </div>
      </form>`;
    document.body.append(dialog);
  }

  /** ask("Delete this?", {ok: "Delete", danger: true}) -> Promise<boolean> */
  window.ask = (message, { ok = "OK", danger = false } = {}) =>
    new Promise((resolve) => {
      if (!dialog) build();
      dialog.querySelector(".confirm-text").textContent = message;
      const okButton = dialog.querySelector("[data-ok]");
      okButton.textContent = ok;
      okButton.className = `btn ${danger ? "danger" : "primary"}`;
      dialog.returnValue = "";
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "yes"), { once: true });
      dialog.showModal();
      okButton.focus();
    });

  // <form data-confirm="..."> and <button data-confirm="..."> ask before submitting.
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    const submitter = event.submitter;
    const message = submitter?.dataset.confirm || form.dataset.confirm;
    if (!message || form.dataset.confirmed) return;
    event.preventDefault();
    const danger = (submitter || form).matches(".danger-text, .danger, [data-danger]") || !!form.querySelector(".danger-text");
    if (await window.ask(message, { ok: submitter?.dataset.ok || form.dataset.ok || "Continue", danger })) {
      form.dataset.confirmed = "1";
      form.requestSubmit(submitter || undefined);
      delete form.dataset.confirmed;
    }
  });
})();
