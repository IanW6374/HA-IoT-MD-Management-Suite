/* Progressive enhancement: keep the real, named form controls and their events. */
const targetPickerState = new Map();
function showWorkspaceError(context, error) {
  let notice = document.getElementById('workspace-error');
  if (!notice) {
    notice = document.createElement('div'); notice.id = 'workspace-error';
    notice.className = 'status error'; notice.setAttribute('role', 'alert');
    document.querySelector('main').prepend(notice);
  }
  const message = context + ': ' + (error.message || String(error));
  if (notice.textContent !== message) notice.textContent = message;
  notice.dataset.context = context;
  notice.hidden = false;
}
function clearWorkspaceError(context) {
  const notice = document.getElementById('workspace-error');
  if (notice?.dataset.context === context) notice.hidden = true;
}
function enhanceTargetPicker(box, title, groupFor) {
  if (!box || box.querySelector('.target-picker')) return;
  const choices = [...box.querySelectorAll('label')].filter(label => label.querySelector('input[type=checkbox]'));
  if (!choices.length) return;
  const saved = targetPickerState.get(box.id) || {open:false, query:'', expanded:new Set()};
  targetPickerState.set(box.id, saved);
  const root = document.createElement('div'); root.className = 'profile-picker target-picker';
  const heading = document.createElement('h3'); heading.textContent = title;
  const dropdown = document.createElement('details'); dropdown.className = 'profile-dropdown'; dropdown.open = saved.open;
  const summary = document.createElement('summary'); summary.setAttribute('aria-label', title);
  const count = document.createElement('span'); summary.append(count);
  const arrow = document.createElement('span'); arrow.textContent = '▾'; arrow.setAttribute('aria-hidden', 'true'); summary.append(arrow);
  const options = document.createElement('div'); options.className = 'profile-options';
  const searchLabel = document.createElement('label'); searchLabel.className = 'profile-option-search';
  searchLabel.append(document.createTextNode('Search ' + title.toLowerCase()));
  const search = document.createElement('input'); search.type = 'search'; search.value = saved.query; search.placeholder = 'Search by name, hostname or group'; searchLabel.append(search);
  const tools = document.createElement('div'); tools.className = 'profile-selection-tools';
  const allLabel = document.createElement('label'); allLabel.className = 'profile-option';
  const all = document.createElement('input'); all.type = 'checkbox';
  const allText = document.createElement('span'); allText.textContent = 'Select all'; allLabel.append(all, allText); tools.append(allLabel);
  const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = 'No matching options.'; empty.hidden = true;
  options.append(searchLabel, tools);
  const groups = new Map();
  for (const label of choices) {
    const input = label.querySelector('input[type=checkbox]');
    const name = groupFor ? groupFor(input.value) : 'Available groups';
    if (!groups.has(name)) groups.set(name, []);
    label.classList.add('profile-option'); groups.get(name).push({label, input});
  }
  const groupRows = [];
  for (const [name, members] of groups) {
    const section = document.createElement('div'); section.className = 'profile-selection-group';
    const row = document.createElement('div'); row.className = 'profile-group-choice';
    const groupLabel = document.createElement('label'); groupLabel.className = 'profile-option';
    const groupCheck = document.createElement('input'); groupCheck.type = 'checkbox';
    const groupName = document.createElement('span'); groupName.textContent = name;
    groupLabel.append(groupCheck, groupName);
    const expand = document.createElement('button'); expand.type = 'button'; expand.className = 'secondary profile-group-expand';
    expand.setAttribute('aria-label', 'Expand ' + name);
    const children = document.createElement('div'); children.className = 'profile-group-settings';
    children.id = box.id + '-group-' + groupRows.length; expand.setAttribute('aria-controls', children.id);
    for (const member of members) children.append(member.label);
    row.append(groupLabel, expand); section.append(row, children); options.append(section);
    const group = {name, members, section, groupCheck, children, expand}; groupRows.push(group);
    groupCheck.addEventListener('change', () => choose(members, groupCheck.checked));
    expand.addEventListener('click', () => {
      if (saved.expanded.has(name)) saved.expanded.delete(name); else saved.expanded.add(name);
      filter();
    });
  }
  options.append(empty); dropdown.append(summary, options);
  const chips = document.createElement('div'); chips.className = 'profile-selection-chips';
  root.append(heading, dropdown, chips); box.replaceChildren(root);
  const enabled = members => members.filter(member => !member.input.matches(':disabled'));
  function checkState(control, members) {
    const eligible = enabled(members), selected = eligible.filter(member => member.input.checked).length;
    control.checked = eligible.length > 0 && selected === eligible.length;
    control.indeterminate = selected > 0 && selected < eligible.length;
    control.disabled = eligible.length === 0;
  }
  function update() {
    const members = groupRows.flatMap(group => group.members), selected = members.filter(member => member.input.checked).length;
    count.textContent = selected + ' of ' + members.length + ' selected';
    checkState(all, members); chips.replaceChildren();
    for (const group of groupRows) {
      checkState(group.groupCheck, group.members);
      const number = group.members.filter(member => member.input.checked).length;
      if (!number) continue;
      const chip = document.createElement('span'); chip.className = 'badge profile-selection-chip';
      const text = document.createElement('span'); text.textContent = group.name + ' · ' + number;
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×';
      remove.setAttribute('aria-label', 'Clear ' + group.name); remove.disabled = !enabled(group.members).length;
      remove.addEventListener('click', () => choose(group.members, false)); chip.append(text, remove); chips.append(chip);
    }
  }
  function choose(members, selected) {
    for (const member of enabled(members)) {
      if (member.input.checked === selected) continue;
      member.input.checked = selected; member.input.dispatchEvent(new Event('change', {bubbles:true}));
    }
    update();
  }
  function filter() {
    saved.query = search.value;
    const terms = saved.query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean); let visible = 0;
    for (const group of groupRows) {
      for (const member of group.members) {
        const text = (group.name + ' ' + member.label.textContent + ' ' + member.input.value).toLocaleLowerCase();
        member.label.hidden = !terms.every(term => text.includes(term));
      }
      const matches = group.members.some(member => !member.label.hidden);
      group.section.hidden = !matches; if (matches) visible++;
      const expanded = terms.length > 0 || saved.expanded.has(group.name);
      group.children.hidden = !expanded; group.expand.textContent = expanded ? '▴' : '▾';
      group.expand.setAttribute('aria-expanded', String(expanded));
    }
    empty.hidden = visible > 0;
  }
  all.addEventListener('change', () => choose(groupRows.flatMap(group => group.members), all.checked));
  root.addEventListener('change', update); search.addEventListener('input', filter);
  dropdown.addEventListener('toggle', () => { if (root.isConnected) saved.open = dropdown.open; });
  root.addEventListener('keydown', event => { if (event.key === 'Escape') { dropdown.open = false; saved.open = false; summary.focus(); } });
  update(); filter();
  if (saved.focusSearch) { search.focus({preventScroll:true}); saved.focusSearch = false; }
}
function enhanceTargetPickers() {
  const groupFor = id => state.devices.find(device => device.id === id)?.cohort || 'Ungrouped';
  for (const prefix of ['', 'backup-']) {
    enhanceTargetPicker(document.getElementById(prefix + 'device-targets'), 'Devices', groupFor);
    enhanceTargetPicker(document.getElementById(prefix + 'cohort-targets'), 'Groups');
  }
}
document.addEventListener('click', event => {
  for (const picker of document.querySelectorAll('.target-picker')) {
    if (picker.contains(event.target)) continue;
    picker.querySelector('details').open = false;
    const saved = targetPickerState.get(picker.parentElement.id); if (saved) saved.open = false;
  }
});
// The renderers preserve the real checkbox values before rebuilding after polling.
const originalRenderTargets = renderTargets;
function rememberTargetFocus() {
  // Native details toggle events are asynchronous; snapshot before detaching.
  for (const picker of document.querySelectorAll('.target-picker')) {
    const saved = targetPickerState.get(picker.parentElement.id);
    if (saved) saved.open = picker.querySelector('details').open;
  }
  const search = document.activeElement;
  if (!search?.matches('.target-picker input[type=search]')) return;
  const box = search.closest('.target-picker').parentElement;
  const saved = targetPickerState.get(box.id); if (saved) saved.focusSearch = true;
}
renderTargets = function() { rememberTargetFocus(); originalRenderTargets(); enhanceTargetPickers(); };
const originalRenderBackupTargets = renderBackupTargets;
renderBackupTargets = function() { rememberTargetFocus(); originalRenderBackupTargets(); enhanceTargetPickers(); };
const originalRefreshBackups = refreshBackups;
refreshBackups = async function(...args) {
  const result = await originalRefreshBackups(...args);
  clearWorkspaceError('Backups could not be refreshed');
  return result;
};
