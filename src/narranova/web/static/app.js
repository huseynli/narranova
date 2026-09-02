document.querySelectorAll("[data-instruction]").forEach((button) => {
  button.addEventListener("click", () => {
    const field = document.querySelector("#instruction");
    if (!field) return;
    field.value = button.dataset.instruction || "";
    field.focus();
    document.querySelectorAll("[data-instruction]").forEach((item) => {
      item.classList.toggle("selected", item === button);
    });
  });
});

const providerSelect = document.querySelector("[data-provider-select]");
const profileSelect = document.querySelector("[data-profile-select]");

function filterProfiles() {
  if (!providerSelect || !profileSelect) return;
  const providerId = providerSelect.value;
  let firstVisible = null;
  Array.from(profileSelect.options).forEach((option) => {
    const visible = option.dataset.provider === providerId;
    option.hidden = !visible;
    option.disabled = !visible;
    if (visible && !firstVisible) firstVisible = option;
  });
  if (!profileSelect.selectedOptions[0] || profileSelect.selectedOptions[0].disabled) {
    profileSelect.value = firstVisible ? firstVisible.value : "";
  }
}

if (providerSelect && profileSelect) {
  providerSelect.addEventListener("change", filterProfiles);
  filterProfiles();
}
