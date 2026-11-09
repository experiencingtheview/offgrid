// The worker fills the digest in when it serves the page. The button stays
// hidden until there is a real hash, so it never invites a copy of the
// "see SHA256SUMS" fallback text.
const DIGEST = /^[0-9a-f]{64}$/;

for (const button of document.querySelectorAll("[data-copy-digest]")) {
  const digest = button.parentElement.querySelector("[data-release-digest]");
  const status = button.parentElement.querySelector(".release-copy-status");
  if (!digest || !status || !DIGEST.test(digest.textContent.trim())) {
    continue;
  }

  button.hidden = false;
  const label = button.textContent;
  let reset;

  button.addEventListener("click", async () => {
    const text = digest.textContent.trim();
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
      status.textContent = "SHA-256 digest copied to the clipboard.";
      clearTimeout(reset);
      reset = setTimeout(() => {
        button.textContent = label;
        status.textContent = "";
      }, 2000);
    } catch {
      // No clipboard API (an insecure context or an old browser): select the
      // digest instead, so the keyboard shortcut works rather than nothing.
      const range = document.createRange();
      range.selectNodeContents(digest);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = "Digest selected: press Ctrl+C or Command+C to copy it.";
    }
  });
}
