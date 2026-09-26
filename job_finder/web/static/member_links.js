const copyButton = document.getElementById("copy-account-link");
const linkField = document.getElementById("account-link");
const copyStatus = document.getElementById("copy-status");

if (copyButton && linkField && copyStatus) {
  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(linkField.value);
      copyStatus.textContent = "Link copied.";
    } catch {
      linkField.focus();
      linkField.select();
      copyStatus.textContent = "Select the link and copy it.";
    }
  });
}
