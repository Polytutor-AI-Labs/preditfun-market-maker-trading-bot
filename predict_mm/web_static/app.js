const form = document.querySelector('#setup-form');
const uiText = PredictI18n.setText;
const translate = PredictI18n.translate;
const accountForm = document.querySelector('#account-form');
const notice = document.querySelector('#notice');
const marketsList = document.querySelector('#markets-list');
const marketTemplate = document.querySelector('#market-template');
const approvalStatus = document.querySelector('#approval-status');
const approvalSteps = document.querySelector('#approval-steps');
const checkApprovalsButton = document.querySelector('#check-approvals');
const setApprovalsButton = document.querySelector('#set-approvals');
const logPanels = [...document.querySelectorAll('.log-preview, .full-log')];
const logRefreshStatus = document.querySelector('#log-refresh-status');
const logRefreshStatusText = document.querySelector('#log-refresh-status-text');
const runDurationEnabled = form.elements.namedItem('run_duration_enabled');
const runDurationHours = form.elements.namedItem('run_duration_hours');
const runDurationMinutes = form.elements.namedItem('run_duration_minutes');
const runDurationFields = document.querySelector('#run-duration-fields');
let formDirty = false;
let approvalActionRunning = false;
let logInteractionPaused = false;
let logRefreshPaused = false;
let logRefreshPending = false;
let logRequestInFlight = false;
let botRunning = false;
let configuredRunDurationSeconds = 0;
let runExpiresAtMs = null;

function populateDurationOptions() {
  runDurationHours.replaceChildren(...Array.from({length: 73}, (_, hour) => {
    const option = document.createElement('option');
    option.value = String(hour);
    uiText(option, `${hour} 小时`);
    return option;
  }));
  runDurationMinutes.replaceChildren(...Array.from({length: 60}, (_, minute) => {
    const option = document.createElement('option');
    option.value = String(minute);
    uiText(option, `${minute} 分钟`);
    return option;
  }));
}

function updateDurationFields({applyDefault = false} = {}) {
  const enabled = runDurationEnabled.checked;
  if (enabled && applyDefault && Number(runDurationHours.value) === 0
      && Number(runDurationMinutes.value) === 0) {
    runDurationHours.value = '1';
  }
  runDurationFields.classList.toggle('inactive', !enabled);
}

function formatRemainingDuration(totalSeconds) {
  const seconds = Math.max(0, Math.ceil(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return [hours, minutes, remainder]
    .map((value) => String(value).padStart(2, '0'))
    .join(':');
}

function updateDurationCountdown() {
  const value = document.querySelector('#expiry-value');
  const note = document.querySelector('#expiry-note');
  if (!configuredRunDurationSeconds) {
    uiText(value, '不限时');
    value.className = 'metric-value';
    uiText(note, '未设置自动停止');
    return;
  }
  if (!botRunning || !runExpiresAtMs) {
    uiText(value, '未启动');
    value.className = 'metric-value warning';
    uiText(note, `启动后倒计时 ${formatRemainingDuration(configuredRunDurationSeconds)}`);
    return;
  }
  const remainingSeconds = Math.max(0, (runExpiresAtMs - Date.now()) / 1000);
  uiText(value, remainingSeconds > 0
    ? formatRemainingDuration(remainingSeconds)
    : '正在停止…');
  value.className = `metric-value ${remainingSeconds > 60 ? 'positive' : 'warning'}`;
  uiText(note, '到期后自动撤单并停止');
}

function switchView(name) {
  const target = document.querySelector(`[data-view-name="${name}"]`) || document.getElementById(name);
  if (!target) return;
  document.querySelectorAll('.view').forEach((view) => view.classList.remove('active'));
  document.querySelectorAll('.nav button[data-view]').forEach((button) => {
    button.classList.toggle('active', button.dataset.view === name);
  });
  target.classList.add('active');
  window.scrollTo({top: 0, behavior: 'smooth'});
}

document.querySelectorAll('.nav button[data-view]').forEach((button) => {
  button.addEventListener('click', () => switchView(button.dataset.view));
});
document.querySelectorAll('[data-view-target]').forEach((button) => {
  button.addEventListener('click', () => switchView(button.dataset.viewTarget));
});

function showNotice(message, kind = 'success') {
  notice.hidden = false;
  notice.className = `notice ${kind}`;
  uiText(notice, message);
}

function isPredictMarketUrl(value) {
  try {
    const url = new URL(value);
    if (!['predict.fun', 'www.predict.fun'].includes(url.hostname.toLowerCase())) return false;
    const parts = url.pathname.split('/').filter(Boolean);
    const marketIndex = parts.indexOf('market');
    return marketIndex >= 0 && Boolean(parts[marketIndex + 1]);
  } catch (_) {
    return false;
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.text();
  let data = {};
  try {
    data = body ? JSON.parse(body) : {};
  } catch (_) {
    if (!response.ok) throw new Error(`服务器错误（${response.status}），请查看运行日志。`);
    throw new Error('服务器返回了无法识别的数据。');
  }
  if (!response.ok) {
    throw new Error(formatErrorDetail(data.detail) || `操作失败（${response.status}），请查看日志。`);
  }
  return data;
}

const validationFieldLabels = {
  market_id: '市场网址 / Market ID',
  market_title: '市场名称',
  outcome: '挂单选项',
  quote_size: '单次挂单数量',
  markets: '做市市场',
  cancel_after_seconds: '挂单存活时间（秒）',
  run_duration_hours: '有效期小时',
  run_duration_minutes: '有效期分钟',
  max_position_per_market: '单市场最大仓位',
  max_total_position: '总最大仓位',
  depth_protection: '买盘深度保护',
  cancel_min_shares: '撤单最低深度',
  cancel_size_multiplier: '撤单数量倍数',
  resume_min_shares: '恢复最低深度',
  resume_size_multiplier: '恢复数量倍数',
  drop_window_seconds: '减量回看窗口',
  drop_percent: '深度下降百分比',
  stable_seconds: '恢复稳定时间',
  cooldown_seconds: '撤单后冷却时间',
};

function validationPath(location = []) {
  return location
    .filter((part) => part !== 'body' && part !== 'markets')
    .map((part) => {
      if (typeof part === 'number') return `市场 ${part + 1}`;
      return validationFieldLabels[part] || String(part);
    })
    .join(' → ');
}

function formatValidationItem(item) {
  if (typeof item === 'string') return item;
  if (!item || typeof item !== 'object') return String(item || '');
  const path = validationPath(Array.isArray(item.loc) ? item.loc : []);
  const translatedMessages = {
    missing: '不能为空',
    string_too_short: '不能为空',
    string_too_long: '内容过长',
    int_parsing: '必须是整数',
    int_type: '必须是整数',
    greater_than_equal: '数值低于允许范围',
    less_than_equal: '数值超过允许范围',
    too_long: '添加的市场数量超过上限',
    too_short: '请至少添加一个市场',
  };
  const message = translatedMessages[item.type]
    || item.msg
    || item.message
    || item.error
    || JSON.stringify(item);
  return path ? `${path}：${message}` : message;
}

function formatErrorDetail(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(formatValidationItem).filter(Boolean).join('；');
  if (detail && typeof detail === 'object') return formatValidationItem(detail);
  return '';
}

function setField(name, value, target = form) {
  const field = target.elements.namedItem(name);
  if (field && value !== undefined && value !== null) field.value = value;
}

function setOutcomeOptions(row, outcomes, selected = '', selectedIndexSet = null) {
  const select = row.querySelector('[data-field="outcome"]');
  const canonicalOutcome = (value) => {
    const text = String(value || '').trim();
    const normalized = text.toUpperCase();
    if (normalized === 'YES') return 'Yes';
    if (normalized === 'NO') return 'No';
    if (['YES_NO', 'YES&NO', 'YES AND NO'].includes(normalized)) return 'YES_NO';
    return text;
  };
  const entries = (outcomes || []).map((outcome) => {
    if (outcome && typeof outcome === 'object') {
      return {
        name: canonicalOutcome(outcome.name),
        indexSet: Number(outcome.index_set) || null,
      };
    }
    const name = canonicalOutcome(outcome);
    return {
      name,
      indexSet: name === 'Yes' ? 1 : name === 'No' ? 2 : null,
    };
  }).filter((entry) => entry.name);
  const uniqueEntries = entries.filter((entry, index) => (
    entries.findIndex((candidate) => candidate.name === entry.name) === index
  ));
  if (!uniqueEntries.length) {
    uniqueEntries.push({name: 'Yes', indexSet: 1}, {name: 'No', indexSet: 2});
  }
  const hasYes = uniqueEntries.some((entry) => entry.indexSet === 1 || entry.name === 'Yes');
  const hasNo = uniqueEntries.some((entry) => entry.indexSet === 2 || entry.name === 'No');
  if (hasYes && hasNo && !uniqueEntries.some((entry) => entry.name === 'YES_NO')) {
    uniqueEntries.push({name: 'YES_NO', indexSet: null});
  }
  const selectedValue = canonicalOutcome(selected);
  if (selectedValue && !uniqueEntries.some((entry) => entry.name === selectedValue)) {
    uniqueEntries.push({name: selectedValue, indexSet: Number(selectedIndexSet) || null});
  }
  select.replaceChildren(...uniqueEntries.map((entry) => {
    const option = document.createElement('option');
    option.value = entry.name;
    if (entry.name === 'YES_NO') uiText(option, selectedOutcomeLabel(entry.name));
    else option.textContent = entry.name;
    if (entry.indexSet) option.dataset.indexSet = String(entry.indexSet);
    return option;
  }));
  select.value = selectedValue && uniqueEntries.some((entry) => entry.name === selectedValue)
    ? selectedValue
    : uniqueEntries[0].name;
}

function renumberMarkets() {
  [...marketsList.querySelectorAll('.market-row')].forEach((row, index) => {
    uiText(row.querySelector('.market-title'), `市场 ${index + 1}`);
    row.querySelector('.remove-market').hidden = index === 0 && marketsList.children.length === 1;
  });
}

function selectedOutcomeLabel(value) {
  const normalized = String(value || '').toUpperCase();
  return normalized === 'YES_NO' ? 'Yes & No（双向）' : value;
}

function updateSelectedMarket(row) {
  const summary = row.querySelector('[data-field="selected_market"]');
  const title = row.dataset.marketTitle || '';
  if (!title) {
    summary.hidden = true;
    summary.replaceChildren();
    return;
  }
  const outcome = row.querySelector('[data-field="outcome"]').value;
  const marketId = row.querySelector('[data-field="market_id"]').value.trim();
  const selection = document.createElement('span');
  uiText(selection, `已选择：${title} · ${selectedOutcomeLabel(outcome)}`);
  const identifier = document.createElement('span');
  identifier.className = 'selected-market-id';
  uiText(identifier, `Market ID：${marketId}`);
  summary.replaceChildren(selection, identifier);
  summary.hidden = false;
}

function marketDisplayName(market) {
  const question = String(market.question || market.title || '').trim();
  const category = String(market.category_title || '').trim();
  if (category && question && !question.toLowerCase().includes(category.toLowerCase())) {
    return `${category} · ${question}`;
  }
  return question || category;
}

function applyResolvedMarket(row, market, selectedOutcome, selectedIndexSet = null) {
  const title = marketDisplayName(market);
  row.querySelector('[data-field="market_id"]').value = market.id;
  row.querySelector('[data-field="market_reference"]').value = title;
  row.dataset.marketTitle = title;
  setOutcomeOptions(row, market.outcomes || ['YES', 'NO'], selectedOutcome, selectedIndexSet);
  updateSelectedMarket(row);
}

function addMarket(market = {}) {
  const row = marketTemplate.content.firstElementChild.cloneNode(true);
  PredictI18n.translateStatic(row);
  const marketIdInput = row.querySelector('[data-field="market_id"]');
  const marketReferenceInput = row.querySelector('[data-field="market_reference"]');
  marketIdInput.value = market.market_id || '';
  row.dataset.marketTitle = market.market_title || '';
  marketReferenceInput.value = market.market_title || market.market_id || '';
  setOutcomeOptions(
    row,
    market.outcomes || ['YES', 'NO'],
    market.outcome || 'YES',
    market.outcome_index_set,
  );
  row.querySelector('[data-field="quote_size"]').value = market.quote_size || '100';
  updateSelectedMarket(row);
  row.querySelector('.remove-market').addEventListener('click', () => {
    if (marketsList.children.length === 1) return;
    row.remove();
    renumberMarkets();
    formDirty = true;
  });
  const resolveButton = row.querySelector('.resolve-market');
  resolveButton.addEventListener('click', () => resolveMarketUrl(row));
  marketReferenceInput.addEventListener('input', () => {
    const value = marketReferenceInput.value.trim();
    marketIdInput.value = /^\d+$/.test(value) ? value : '';
    row.dataset.marketTitle = '';
    updateSelectedMarket(row);
  });
  marketReferenceInput.addEventListener('change', () => {
    if (isPredictMarketUrl(marketReferenceInput.value.trim())) resolveMarketUrl(row);
  });
  row.querySelector('[data-field="outcome"]').addEventListener('change', () => updateSelectedMarket(row));
  marketsList.append(row);
  renumberMarkets();
}

function renderMarketLookup(row, result) {
  const container = row.querySelector('[data-field="market_lookup"]');
  container.replaceChildren();
  container.hidden = false;
  const message = document.createElement('p');
  uiText(message, result.message);
  container.append(message);
  (result.matches || []).forEach((market) => {
    const group = document.createElement('div');
    group.className = 'market-match-group';
    const title = document.createElement('p');
    const category = market.category_title ? `${market.category_title} · ` : '';
    title.textContent = `${category}${market.question}${market.trading_status ? ` · ${market.trading_status}` : ''}`;
    title.title = `Market ID: ${market.id}`;
    group.append(title);
    const outcomes = market.outcomes?.length ? market.outcomes : ['YES', 'NO'];
    outcomes.forEach((outcome) => {
      const outcomeName = typeof outcome === 'object' ? outcome.name : outcome;
      const outcomeIndexSet = typeof outcome === 'object' ? outcome.index_set : null;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'market-match';
      uiText(button, `选择「${selectedOutcomeLabel(outcomeName)}」挂单`);
      button.addEventListener('click', () => {
        applyResolvedMarket(row, market, outcomeName, outcomeIndexSet);
        container.hidden = true;
        formDirty = true;
        showNotice(`已选择 ${marketDisplayName(market)} · ${selectedOutcomeLabel(outcomeName)}`);
      });
      group.append(button);
    });
    const hasYes = outcomes.some((outcome) => (
      (typeof outcome === 'object' && Number(outcome.index_set) === 1)
      || String(typeof outcome === 'object' ? outcome.name : outcome).toUpperCase() === 'YES'
    ));
    const hasNo = outcomes.some((outcome) => (
      (typeof outcome === 'object' && Number(outcome.index_set) === 2)
      || String(typeof outcome === 'object' ? outcome.name : outcome).toUpperCase() === 'NO'
    ));
    if (hasYes && hasNo) {
      const bothButton = document.createElement('button');
      bothButton.type = 'button';
      bothButton.className = 'market-match';
      uiText(bothButton, '选择「Yes & No」双向挂单');
      bothButton.addEventListener('click', () => {
        applyResolvedMarket(row, market, 'YES_NO');
        container.hidden = true;
        formDirty = true;
        showNotice(`已选择 ${marketDisplayName(market)} · ${selectedOutcomeLabel('YES_NO')}`);
      });
      group.append(bothButton);
    }
    container.append(group);
  });
}

async function resolveMarketUrl(row) {
  const input = row.querySelector('[data-field="market_reference"]');
  const value = input.value.trim();
  if (!isPredictMarketUrl(value)) {
    showNotice('请先粘贴完整的 Predict.fun 市场网址。', 'error');
    return;
  }
  const button = row.querySelector('.resolve-market');
  button.disabled = true;
  uiText(button, '识别中…');
  try {
    const result = await request('/api/resolve-market', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({market_url: value}),
    });
    if (result.market_id) {
      row.querySelector('[data-field="market_id"]').value = result.market_id;
      formDirty = true;
      showNotice(result.message);
    } else {
      renderMarketLookup(row, result);
    }
  } catch (error) {
    showNotice(error.message, 'error');
  } finally {
    button.disabled = false;
    uiText(button, '识别网址');
  }
}

function renderMarkets(markets = []) {
  marketsList.replaceChildren();
  (markets.length ? markets : [{}]).forEach(addMarket);
}

function collectMarkets() {
  return [...marketsList.querySelectorAll('.market-row')].map((row) => {
    const outcomeSelect = row.querySelector('[data-field="outcome"]');
    const rawIndexSet = outcomeSelect.selectedOptions[0]?.dataset.indexSet;
    return {
      market_id: row.querySelector('[data-field="market_id"]').value.trim(),
      market_title: row.dataset.marketTitle || '',
      outcome: outcomeSelect.value,
      outcome_index_set: rawIndexSet ? Number(rawIndexSet) : null,
      quote_size: row.querySelector('[data-field="quote_size"]').value.trim(),
    };
  });
}

function formatOrderAge(value) {
  const totalSeconds = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours) return `${hours}小时 ${minutes}分 ${seconds}秒`;
  if (minutes) return `${minutes}分 ${seconds}秒`;
  return `${seconds}秒`;
}

function renderOpenOrders(orders = []) {
  const list = document.querySelector('#open-orders-list');
  const summary = document.querySelector('#open-orders-summary');
  list.replaceChildren();
  const marketCount = new Set(orders.map((order) => order.market_id)).size;
  uiText(summary, orders.length ? `每 2 秒刷新 · ${orders.length} 笔` : '暂无挂单');
  document.querySelector('#open-order-value').textContent = orders.length;
  uiText(document.querySelector('#open-order-note'), orders.length
    ? `分布在 ${marketCount} 个市场`
    : '暂无机器人管理的挂单');
  if (!orders.length) {
    const row = document.createElement('tr');
    row.className = 'empty-row';
    const cell = document.createElement('td');
    cell.colSpan = 5;
    uiText(cell, '机器人当前没有管理中的挂单。');
    row.append(cell);
    list.append(row);
    return;
  }
  orders.forEach((order) => {
    const row = document.createElement('tr');
    const marketCell = document.createElement('td');
    const market = document.createElement('div');
    market.className = 'market-cell';
    const token = document.createElement('div');
    token.className = 'token';
    token.textContent = selectedOutcomeLabel(order.outcome) || '—';
    const marketInfo = document.createElement('div');
    const marketName = document.createElement('strong');
    if (order.market_title) marketName.textContent = order.market_title;
    else uiText(marketName, '未命名市场');
    marketName.title = `Market ID: ${order.market_id}`;
    const orderId = document.createElement('small');
    uiText(orderId, `${selectedOutcomeLabel(order.outcome)} · 订单 ${order.order_id}`);
    marketInfo.append(marketName, orderId);
    market.append(token, marketInfo);
    marketCell.append(market);

    const sideCell = document.createElement('td');
    const side = document.createElement('span');
    side.className = `side-badge ${order.side === 'sell' ? 'sell' : ''}`;
    side.textContent = `${String(order.side || '').toUpperCase()} ${selectedOutcomeLabel(order.outcome) || ''}`.trim();
    sideCell.append(side);
    const price = document.createElement('td');
    price.className = 'order-price';
    price.textContent = order.price;
    const size = document.createElement('td');
    size.textContent = order.size;
    const age = document.createElement('td');
    uiText(age, formatOrderAge(order.age_seconds));
    row.append(marketCell, sideCell, price, size, age);
    list.append(row);
  });
}

function formatBalance(value) {
  const balance = Number(value);
  if (!Number.isFinite(balance)) return String(value);
  return balance.toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 4});
}

async function refreshBalance() {
  const value = document.querySelector('#balance-value');
  const note = document.querySelector('#balance-note');
  try {
    const balance = await request('/api/balance');
    uiText(value, `${formatBalance(balance.balance)} ${balance.asset}`);
    uiText(note, '链上余额 · 每 30 秒刷新');
    value.title = balance.account_address;
  } catch (error) {
    uiText(value, '余额不可用');
    uiText(note, error.message);
    value.removeAttribute('title');
  }
}

function renderApprovals(result) {
  uiText(approvalStatus, result.ready
    ? '授权完整'
    : `缺少 ${result.missing} 项授权`);
  approvalStatus.className = result.ready ? 'approval-ready' : 'approval-missing';
  approvalSteps.replaceChildren(...(result.steps || []).map((step) => {
    const row = document.createElement('div');
    row.className = 'approval-step';
    const label = document.createElement('span');
    uiText(label, step.type === 'ERC20_ALLOWANCE' ? 'USDT 交易额度' : '预测份额交易权限');
    const state = document.createElement('strong');
    uiText(state, step.satisfied ? '已授权' : '待授权');
    state.className = step.satisfied ? 'approval-ready' : 'approval-missing';
    row.append(label, state);
    return row;
  }));
  const gasWallet = document.querySelector('#gas-wallet');
  if (result.gas_wallet_address) {
    gasWallet.hidden = false;
    gasWallet.replaceChildren();
    const balance = document.createElement('strong');
    uiText(balance, `授权 Gas 余额：${formatBalance(result.gas_balance)} ${result.gas_asset}`);
    balance.className = Number(result.gas_balance) > 0 ? 'approval-ready' : 'approval-missing';
    const address = document.createElement('code');
    address.textContent = result.gas_wallet_address;
    address.setAttribute('data-i18n-title', '这是 Privy/EOA 签名钱包地址，不是 Predict Account Address');
    address.title = translate(address.getAttribute('data-i18n-title'));
    const addressLabel = document.createElement('span');
    uiText(addressLabel, '授权 Gas 钱包地址：');
    gasWallet.append(balance, addressLabel, address);
  } else {
    gasWallet.hidden = true;
    gasWallet.replaceChildren();
  }
}

async function refreshApprovals() {
  checkApprovalsButton.disabled = true;
  uiText(approvalStatus, '检查中…');
  approvalStatus.className = 'muted';
  try {
    const result = await request('/api/approvals');
    renderApprovals(result);
    return result;
  } catch (error) {
    uiText(approvalStatus, '检查失败');
    approvalStatus.className = 'approval-missing';
    approvalSteps.replaceChildren();
    document.querySelector('#gas-wallet').hidden = true;
    showNotice(error.message, 'error');
    return null;
  } finally {
    checkApprovalsButton.disabled = approvalActionRunning;
  }
}

async function refreshStatus() {
  try {
    const status = await request('/api/status');
    const mode = status.dry_run ? '模拟运行' : '实盘模式';
    const badge = document.querySelector('#mode-badge');
    uiText(badge, mode);
    badge.className = `pill ${status.dry_run ? '' : 'live'}`;
    const runStatus = document.querySelector('#run-status');
    botRunning = status.running;
    configuredRunDurationSeconds = Number(status.run_duration_seconds) || 0;
    const parsedExpiry = status.run_expires_at ? Date.parse(status.run_expires_at) : NaN;
    runExpiresAtMs = status.running && Number.isFinite(parsedExpiry) ? parsedExpiry : null;
    updateDurationCountdown();
    uiText(runStatus, status.running ? '运行中' : (status.configured ? '已停止' : '等待配置'));
    runStatus.className = `metric-value ${status.running ? 'positive' : 'warning'}`;
    uiText(document.querySelector('#run-status-note'), status.running
      ? '机器人正在管理订单'
      : (status.configured ? '配置已加载，可以启动' : '请先保存账户和挂单设置'));
    const markets = status.markets || [];
    uiText(document.querySelector('#market-value'), markets.length ? `${markets.length} 个市场` : '—');
    document.querySelector('#market-count').textContent = markets.length;
    uiText(document.querySelector('#configured-market-summary'), markets.length
      ? `已配置 ${markets.length} 个市场`
      : '尚未配置市场');
    uiText(document.querySelector('#configured-note'), markets.length
      ? (status.dry_run ? '当前为模拟运行' : '当前为实盘模式')
      : '等待加载配置');
    document.querySelector('#risk-total').textContent = status.max_total_position;
    document.querySelector('#risk-market').textContent = status.max_position_per_market;
    uiText(document.querySelector('#risk-emergency'), status.emergency_exit_on_buy_fill ? '已启用' : '未启用');
    document.querySelector('#risk-emergency').className = status.emergency_exit_on_buy_fill ? 'positive' : 'warning';
    renderOpenOrders(status.open_orders || []);
    document.querySelector('#start-button').disabled = !status.configured || status.running;
    document.querySelector('#stop-button').disabled = !status.running;
    document.querySelector('#cancel-button').disabled = !status.configured;
    setApprovalsButton.disabled = status.running || approvalActionRunning;
    const isEditing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName);
    if (!formDirty && !isEditing) {
      if (markets.length) renderMarkets(markets);
      setField('cancel_after_seconds', status.cancel_after_seconds);
      const depth = status.depth_protection || {};
      for (const [key, value] of Object.entries(depth)) {
        if (key === 'enabled') form.elements.namedItem('depth_enabled').checked = value;
        else setField(`depth_${key}`, value);
      }
      setField('max_position_per_market', status.max_position_per_market);
      setField('max_total_position', status.max_total_position);
      const durationSeconds = Number(status.run_duration_seconds) || 0;
      runDurationEnabled.checked = Boolean(status.run_duration_enabled);
      runDurationHours.value = String(Math.floor(durationSeconds / 3600));
      runDurationMinutes.value = String(Math.floor((durationSeconds % 3600) / 60));
      updateDurationFields();
      form.elements.namedItem('dry_run').checked = status.dry_run;
      form.elements.namedItem('emergency_exit_on_buy_fill').checked = status.emergency_exit_on_buy_fill;
    }
    if (!isEditing) {
      setField('predict_account_address', status.account_address, accountForm);
      setField('log_level', status.log_level, accountForm);
    }
    uiText(document.querySelector('#secret-status'), [
      status.api_key_set && 'API Key 已保存',
      status.jwt_token_set && 'JWT 已保存',
      status.private_key_set && '私钥已保存',
    ].filter(Boolean).join(' · ') || '尚未保存实盘密钥');
    const accountReady = status.api_key_set && status.private_key_set;
    const accountBadge = document.querySelector('#account-badge');
    uiText(accountBadge, accountReady ? '账户已连接' : '账户配置不完整');
    accountBadge.className = `pill ${accountReady ? '' : 'live'}`;
    uiText(document.querySelector('#sidebar-account'), status.account_address
      ? `${status.account_address.slice(0, 8)}…${status.account_address.slice(-4)}`
      : (accountReady ? 'EOA 账户已连接' : '账户尚未配置'));
    if (status.last_error) showNotice(`机器人停止：${status.last_error}`, 'error');
  } catch (error) {
    showNotice(error.message, 'error');
  }
}

function selectionIsInsideLogs() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return false;
  return logPanels.some((panel) => (
    (selection.anchorNode && panel.contains(selection.anchorNode))
    || (selection.focusNode && panel.contains(selection.focusNode))
  ));
}

function updateLogRefreshPauseState() {
  const paused = logInteractionPaused || selectionIsInsideLogs();
  if (paused === logRefreshPaused) return;
  logRefreshPaused = paused;
  logPanels.forEach((panel) => panel.classList.toggle('paused', paused));
  logRefreshStatus.classList.toggle('paused', paused);
  uiText(logRefreshStatusText, paused
    ? '已暂停（点击日志外恢复）'
    : '每 2 秒更新');
  if (!paused) refreshLogs({force: true});
}

async function refreshLogs({force = false} = {}) {
  if (!force && logRefreshPaused) {
    logRefreshPending = true;
    return;
  }
  if (logRequestInFlight) {
    logRefreshPending = true;
    return;
  }
  logRequestInFlight = true;
  try {
    const { lines } = await request('/api/logs');
    if (!force && logRefreshPaused) {
      logRefreshPending = true;
      return;
    }
    const logs = document.querySelector('#logs');
    logs.removeAttribute('data-i18n-source');
    logs.textContent = lines.length ? lines.join('\n') : '暂无运行日志。';
    if (!lines.length) uiText(logs, '暂无运行日志。');
    logs.scrollTop = logs.scrollHeight;
    const preview = document.querySelector('#dashboard-logs');
    preview.removeAttribute('data-i18n-source');
    preview.textContent = lines.length ? lines.slice(-6).join('\n') : '暂无运行日志。';
    if (!lines.length) uiText(preview, '暂无运行日志。');
    preview.scrollTop = preview.scrollHeight;
    logRefreshPending = false;
  } catch (error) {
    showNotice(error.message, 'error');
  } finally {
    logRequestInFlight = false;
    if (!logRefreshPaused && logRefreshPending) {
      logRefreshPending = false;
      queueMicrotask(() => refreshLogs({force: true}));
    }
  }
}

logPanels.forEach((panel) => {
  panel.addEventListener('focus', () => {
    logInteractionPaused = true;
    updateLogRefreshPauseState();
  });
  panel.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    window.getSelection()?.removeAllRanges();
    logInteractionPaused = false;
    panel.blur();
    updateLogRefreshPauseState();
  });
});

document.addEventListener('pointerdown', (event) => {
  if (event.target.closest?.('.log-preview, .full-log')) {
    logInteractionPaused = true;
    updateLogRefreshPauseState();
    return;
  }
  logInteractionPaused = false;
  requestAnimationFrame(updateLogRefreshPauseState);
});
document.addEventListener('selectionchange', updateLogRefreshPauseState);

document.querySelectorAll('#add-market-button, #add-market-bottom-button').forEach((button) => {
  button.addEventListener('click', () => {
    addMarket();
    formDirty = true;
  });
});
form.addEventListener('input', () => { formDirty = true; });
form.addEventListener('change', () => { formDirty = true; });
runDurationEnabled.addEventListener('change', () => updateDurationFields({applyDefault: true}));
[runDurationHours, runDurationMinutes].forEach((select) => {
  select.addEventListener('change', () => {
    runDurationEnabled.checked = true;
    updateDurationFields();
  });
});
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(form));
  values.depth_protection = {enabled: form.elements.namedItem('depth_enabled').checked};
  for (const [key, value] of Object.entries(values)) {
    if (key.startsWith('depth_') && key !== 'depth_protection') {
      if (key !== 'depth_enabled') values.depth_protection[key.slice(6)] = value;
      delete values[key];
    }
  }
  values.markets = collectMarkets();
  values.dry_run = form.elements.namedItem('dry_run').checked;
  values.emergency_exit_on_buy_fill = form.elements.namedItem('emergency_exit_on_buy_fill').checked;
  values.run_duration_enabled = runDurationEnabled.checked;
  values.run_duration_hours = Number(runDurationHours.value);
  values.run_duration_minutes = Number(runDurationMinutes.value);
  try {
    const result = await request('/api/setup', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values),
    });
    formDirty = false;
    showNotice(result.message);
    await refreshStatus();
  } catch (error) {
    showNotice(error.message, 'error');
  }
});

accountForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(accountForm));
  try {
    const result = await request('/api/account', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values),
    });
    ['api_key', 'private_key'].forEach((name) => {
      accountForm.elements.namedItem(name).value = '';
    });
    showNotice(result.message);
    await refreshStatus();
    await refreshApprovals();
  } catch (error) {
    showNotice(error.message, 'error');
  }
});

checkApprovalsButton.addEventListener('click', refreshApprovals);
setApprovalsButton.addEventListener('click', async () => {
  const confirmed = confirm(
    translate('设置交易授权会使用已保存的 Privy/EOA 私钥发送链上授权交易，可能产生网络 Gas 费用，但不会创建订单。是否继续？'),
  );
  if (!confirmed) return;
  approvalActionRunning = true;
  checkApprovalsButton.disabled = true;
  setApprovalsButton.disabled = true;
  uiText(approvalStatus, '正在设置…');
  try {
    const result = await request('/api/approvals', {method: 'POST'});
    showNotice(result.message);
    await refreshApprovals();
  } catch (error) {
    uiText(approvalStatus, '设置失败');
    approvalStatus.className = 'approval-missing';
    showNotice(error.message, 'error');
  } finally {
    approvalActionRunning = false;
    checkApprovalsButton.disabled = false;
    await refreshStatus();
  }
});

document.querySelector('#start-button').addEventListener('click', async () => {
  try { showNotice((await request('/api/start', {method: 'POST'})).message); await refreshStatus(); } catch (error) { showNotice(error.message, 'error'); }
});
document.querySelector('#stop-button').addEventListener('click', async () => {
  try { showNotice((await request('/api/stop', {method: 'POST'})).message); await refreshStatus(); } catch (error) { showNotice(error.message, 'error'); }
});
document.querySelector('#cancel-button').addEventListener('click', async () => {
  if (!confirm(translate('确定要撤销所有已配置市场的订单吗？'))) return;
  try { showNotice((await request('/api/cancel-all', {method: 'POST'})).message); } catch (error) { showNotice(error.message, 'error'); }
});
document.querySelector('#refresh-logs').addEventListener('click', () => refreshLogs({force: true}));

document.addEventListener('language-save-error', () => {
  showNotice('语言偏好未能保存，请稍后重试。', 'error');
});

populateDurationOptions();
updateDurationFields();
renderMarkets();
refreshStatus();
refreshLogs();
refreshBalance();
setInterval(() => { refreshStatus(); refreshLogs(); }, 2000);
setInterval(updateDurationCountdown, 1000);
setInterval(refreshBalance, 30000);
