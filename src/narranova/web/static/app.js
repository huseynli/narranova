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
  const providerKind = providerSelect.selectedOptions[0]?.dataset.kind || "";
  let firstVisible = null;
  Array.from(profileSelect.options).forEach((option) => {
    const visible =
      option.dataset.provider === providerId ||
      (!option.dataset.provider && option.dataset.kind === providerKind);
    option.hidden = !visible;
    option.disabled = !visible;
    if (visible && !firstVisible) firstVisible = option;
  });
  if (!profileSelect.selectedOptions[0] || profileSelect.selectedOptions[0].disabled) {
    profileSelect.value = firstVisible ? firstVisible.value : "";
  }
  document.querySelectorAll("[data-default-kind]").forEach((card) => {
    card.hidden = card.dataset.defaultKind !== providerKind;
  });
}

if (providerSelect && profileSelect) {
  providerSelect.addEventListener("change", filterProfiles);
  filterProfiles();
}

const studioProvider = document.querySelector("[data-studio-provider]");

function updateStudioModules() {
  if (!studioProvider) return;
  const option = studioProvider.selectedOptions[0];
  const instructions = option?.dataset.instructions !== "false";
  const reference = option?.dataset.reference !== "false";
  document.querySelectorAll('[data-studio-module="instructions"]').forEach((module) => {
    module.hidden = !instructions;
    module.querySelectorAll("textarea, input, select").forEach((field) => {
      field.disabled = !instructions;
    });
  });
  document.querySelectorAll('[data-studio-module="reference"]').forEach((module) => {
    module.hidden = !reference;
    module.querySelectorAll("textarea, input, select").forEach((field) => {
      field.disabled = !reference;
    });
  });
}

if (studioProvider) {
  studioProvider.addEventListener("change", updateStudioModules);
  updateStudioModules();
}
