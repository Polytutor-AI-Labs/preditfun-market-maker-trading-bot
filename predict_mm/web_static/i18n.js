/* Interface translations only. Market names, outcome values, and raw logs stay intact. */
(() => {
  'use strict';
  const messages = {
    'Predict.fun 自动挂单机器人': 'Predict.fun Market Making Bot',
    '控制中心': 'Control center',
    '控制台页面': 'Dashboard pages',
    '总览 Dashboard': 'Dashboard',
    '账户设置': 'Account settings',
    '挂单设置': 'Quote settings',
    '运行日志': 'Activity logs',
    '网页服务正常': 'Dashboard online',
    '账户状态检查中': 'Checking account',
    '页面每 2 秒更新': 'Updates every 2 seconds',
    '界面语言': 'Interface language',
    '监控机器人、挂单与风险状态': 'Monitor your bot, orders, and risk settings',
    '正在检查配置': 'Checking configuration',
    '启动机器人': 'Start bot',
    '停止并撤单': 'Stop & cancel orders',
    '机器人状态': 'Bot status',
    '未配置': 'Not configured',
    '正在读取运行状态': 'Loading bot status',
    '账户地址上的链上 USDT 余额；不含预测持仓的市值。': 'On-chain USDT balance at the account address; excludes the value of prediction positions.',
    '账户 USDT 余额': 'Account USDT balance',
    '读取中…': 'Loading…',
    '每 30 秒刷新': 'Updates every 30 seconds',
    '当前开放订单': 'Open orders',
    '暂无机器人管理的挂单': 'No orders managed by the bot',
    '已配置市场': 'Configured markets',
    '等待加载配置': 'Waiting for configuration',
    '挂单有效期剩余': 'Session time remaining',
    '正在读取有效期设置': 'Loading session duration',
    '当前挂单': 'Current orders',
    '机器人当前管理的开放订单': 'Open orders currently managed by the bot',
    '暂无挂单': 'No open orders',
    '市场': 'Market',
    '方向': 'Side',
    '价格': 'Price',
    '数量': 'Shares',
    '挂单时间': 'Order age',
    '风险状态': 'Risk settings',
    '当前保存的仓位与成交保护': 'Saved position limits and fill protection',
    '已加载': 'Loaded',
    '总最大仓位': 'Total position limit',
    '单市场最大仓位': 'Position limit per market',
    '买单被吃单后立即卖出': 'Sell immediately after a buy fill',
    '最新日志': 'Recent logs',
    '最近的运行信息': 'Recent bot activity',
    '全部日志': 'All logs',
    '等待日志…': 'Waiting for logs…',
    '撤销所有市场订单': 'Cancel orders in all markets',
    '账户信息单独保存，密钥不会显示完整内容': 'Save account settings separately; saved keys are not shown in full',
    '正在检查账户': 'Checking account',
    'Predict.fun 账户': 'Predict.fun account',
    '保存后自动生成并维护 JWT': 'JWT is generated and maintained automatically after saving',
    '尚未保存实盘密钥': 'Live trading keys not saved',
    '留空则保留原值': 'Leave blank to keep the saved value',
    '与申请时使用的 Predict 账户相匹配': 'Use the Predict account associated with your API Key',
    '钱包 Private Key': 'Wallet private key',
    'EOA 私钥；Predict Account 请填 Privy 导出的私钥': 'EOA private key, or the exported Privy key for a Predict account',
    '不会写入日志或上传 GitHub': 'Not written to logs or uploaded to GitHub',
    '使用 Predict Account 时填写存款地址；EOA 留空': 'Predict account deposit address; leave blank for an EOA',
    '日志级别': 'Log level',
    '保存时会自动生成 JWT：填写 Predict Account Address 时使用 Privy Wallet 私钥生成智能钱包签名，否则使用 EOA 私钥。私钥只保存在服务器。': 'Saving generates a JWT automatically. For a Predict Account Address, the Privy wallet key signs for the smart wallet; otherwise the EOA key is used. The private key stays on the computer running the bot.',
    '保存账户设置': 'Save account settings',
    '交易授权': 'Trading approvals',
    '实盘下单前需要；检查操作不会创建订单': 'Required for live trading; checking does not place orders',
    '尚未检查': 'Not checked',
    '设置授权会发送链上交易，并由 Privy 签名钱包（EOA 模式为 EOA 钱包）支付 BNB Chain Gas。': 'Setting approvals sends on-chain transactions. The Privy signing wallet (or EOA in EOA mode) pays BNB Chain gas.',
    '检查交易授权': 'Check approvals',
    '设置交易授权': 'Set approvals',
    '管理做市市场、挂单数量和刷新策略': 'Manage markets, order sizes, and refresh rules',
    '＋ 添加市场': '＋ Add market',
    '做市市场': 'Markets to quote',
    '选择 Yes 或 No 只挂对应选项；选择 Yes & No 才会双向挂单': 'Choose Yes or No to quote that outcome only; choose Yes & No to quote both',
    '尚未配置市场': 'No markets configured',
    '仓位与刷新': 'Positions & refresh',
    '所有市场共用的风险限制': 'Risk limits shared across all markets',
    '挂单存活时间（秒）': 'Order lifetime (seconds)',
    '价格逼近时会立即撤单，不受此时间限制。达到设定时间后，若买一、卖一和目标挂单价格均未变化，订单最多额外保留一个相同周期；第二个周期结束后撤单并按最新盘口重新挂单。': 'Price proximity triggers immediate cancellation regardless of this lifetime. If the best bid, best ask, and target quote are unchanged at the end of the lifetime, an order may stay for one extra period of the same length. It is canceled and requoted after the second period.',
    '设置挂单有效期': 'Set session duration',
    '从每次启动机器人时开始倒计时；到期后撤销全部订单并停止机器人': 'The countdown starts each time the bot starts. When it ends, cancel all orders and stop the bot',
    '是否启用挂单有效期': 'Enable session duration',
    '挂单有效期': 'Session duration',
    '有效期小时': 'Duration hours',
    '有效期分钟': 'Duration minutes',
    '包括 Yes 与 No；成交保护启用后会停止对应市场继续报价': 'Applies to Yes and No; fill protection pauses quoting in the affected market',
    '实盘专用。买单被吃单（包括部分成交）时，机器人会撤掉该市场其他订单，并提交非 post-only 限价卖单。市场 tick 为 0.001 时使用 0.001，否则使用 0.01。这可能会造成损失。': 'Live trading only. After a buy fill, including a partial fill, cancel other orders in that market and submit a non-post-only limit sell. The limit is 0.001 when the market tick is 0.001, otherwise 0.01. This may cause a loss.',
    '模拟运行': 'Dry run',
    '开启后只计算报价和写入日志，不会创建真实订单': 'Calculate quotes and record logs without placing real orders',
    '买盘深度保护': 'Bid depth protection',
    '前方买盘过少或快速减少时优先撤单；恢复后仍由普通批次重新挂单': 'Prioritize cancellation when bids ahead are thin or shrink quickly; resume through normal scheduling after recovery',
    '深度保护阈值': 'Depth protection thresholds',
    'Q 为单次挂单份额，D 为价格严格高于你的买价的外部买单总份额，不含自己的挂单。撤单与恢复阈值均取“最低份额”和“倍数 × Q”的较大值。Yes、No 分别计算；深度保护也适用于最低 tick 挂单。': 'Q is your order size. D is the total external bid size strictly above your buy price, excluding your own orders. Cancellation and recovery thresholds use the greater of the minimum shares and the multiplier × Q. Yes and No are checked separately, including orders at the minimum tick.',
    '撤单最低深度（份）': 'Minimum depth before cancellation (shares)',
    '撤单数量倍数': 'Cancellation size multiplier',
    '减量回看窗口（秒）': 'Depth decline lookback (seconds)',
    '恢复最低深度（份）': 'Minimum depth to resume (shares)',
    '恢复数量倍数': 'Recovery size multiplier',
    '深度下降百分比（%）': 'Depth decline (%)',
    '恢复稳定时间（秒）': 'Recovery stability (seconds)',
    '撤单后冷却（秒）': 'Cooldown after cancellation (seconds)',
    '快速减量须同时满足下降比例和剩余深度低于恢复阈值；回看窗口不是轮询间隔。恢复须经过冷却并稳定达标，提交前会再次检查。此策略可能减少挂单，不能保证避免成交。': 'A rapid decline must meet both the percentage threshold and the low remaining-depth condition. The lookback is not the polling interval. Resuming requires cooldown and stable depth, with another check before submission. This may reduce quoting and cannot guarantee that orders will not fill.',
    '保存挂单设置': 'Save quote settings',
    '查看机器人、订单和成交保护的完整记录': 'View the full record of bot activity, orders, and fill protection',
    '每 2 秒更新': 'Updates every 2 seconds',
    '刷新': 'Refresh',
    '实时日志': 'Live logs',
    '点击日志或选择文字时暂停更新；点击日志框外恢复': 'Click the logs or select text to pause updates; click outside to resume',
    '移除': 'Remove',
    'Predict.fun 市场网址': 'Predict.fun market URL',
    '粘贴完整的 Predict.fun 市场网址并点击“识别网址”。选择后，这里会显示市场名称；程序会在内部保存下单所需的 Market ID。': 'Paste a full Predict.fun market URL and click Identify URL. After selection, the market name appears here; the bot keeps the Market ID for trading.',
    '粘贴 Predict.fun 市场网址': 'Paste a Predict.fun market URL',
    '识别网址': 'Identify URL',
    '挂单选项': 'Outcome to quote',
    'Yes & No（双向）': 'Yes & No (both)',
    '单次挂单数量': 'Order size (shares)',
    '不限时': 'No time limit',
    '未设置自动停止': 'Automatic stop not set',
    '未启动': 'Not started',
    '正在停止…': 'Stopping…',
    '到期后自动撤单并停止': 'Cancel orders and stop when the timer ends',
    '服务器返回了无法识别的数据。': 'The server returned an unrecognized response.',
    '市场网址 / Market ID': 'Market URL / Market ID',
    '市场名称': 'Market name',
    '撤单最低深度': 'Minimum depth before cancellation',
    '恢复最低深度': 'Minimum depth to resume',
    '减量回看窗口': 'Depth decline lookback',
    '深度下降百分比': 'Depth decline percentage',
    '恢复稳定时间': 'Recovery stability time',
    '撤单后冷却时间': 'Cooldown after cancellation',
    '不能为空': 'Required',
    '内容过长': 'Value is too long',
    '必须是整数': 'Must be an integer',
    '数值低于允许范围': 'Below the allowed minimum',
    '数值超过允许范围': 'Above the allowed maximum',
    '添加的市场数量超过上限': 'Too many markets added',
    '请至少添加一个市场': 'Add at least one market',
    '选择「Yes & No」双向挂单': 'Quote both Yes & No',
    '请先粘贴完整的 Predict.fun 市场网址。': 'Paste a full Predict.fun market URL first.',
    '识别中…': 'Identifying…',
    '机器人当前没有管理中的挂单。': 'The bot is not currently managing any open orders.',
    '未命名市场': 'Unnamed market',
    '链上余额 · 每 30 秒刷新': 'On-chain balance · updates every 30 seconds',
    '余额不可用': 'Unavailable',
    '授权完整': 'Approvals complete',
    'USDT 交易额度': 'USDT trading allowance',
    '预测份额交易权限': 'Prediction share approval',
    '已授权': 'Approved',
    '待授权': 'Approval needed',
    '这是 Privy/EOA 签名钱包地址，不是 Predict Account Address': 'Privy/EOA signing wallet address, not the Predict Account Address',
    '授权 Gas 钱包地址：': 'Gas-paying wallet address: ',
    '检查中…': 'Checking…',
    '检查失败': 'Check failed',
    '实盘模式': 'Live trading',
    '运行中': 'Running',
    '已停止': 'Stopped',
    '等待配置': 'Setup required',
    '机器人正在管理订单': 'The bot is managing orders',
    '配置已加载，可以启动': 'Configuration loaded; ready to start',
    '请先保存账户和挂单设置': 'Save account and quote settings first',
    '当前为模拟运行': 'Dry-run mode selected',
    '当前为实盘模式': 'Live-trading mode selected',
    '已启用': 'Enabled',
    '未启用': 'Disabled',
    'API Key 已保存': 'API Key saved',
    'JWT 已保存': 'JWT saved',
    '私钥已保存': 'Private key saved',
    '账户已连接': 'Account connected',
    '账户配置不完整': 'Account setup incomplete',
    'EOA 账户已连接': 'EOA account connected',
    '账户尚未配置': 'Account not configured',
    '已暂停（点击日志外恢复）': 'Paused (click outside logs to resume)',
    '暂无运行日志。': 'No activity logs yet.',
    '设置交易授权会使用已保存的 Privy/EOA 私钥发送链上授权交易，可能产生网络 Gas 费用，但不会创建订单。是否继续？': 'Set trading approvals using the saved Privy/EOA key? This sends on-chain approval transactions and may incur gas fees. It does not place orders.',
    '正在设置…': 'Setting approvals…',
    '设置失败': 'Setup failed',
    '确定要撤销所有已配置市场的订单吗？': 'Cancel all orders in the configured markets?',
    '语言偏好未能保存，请稍后重试。': 'Could not save your language preference. Please try again.',
    '请先完成网页配置。': 'Complete the dashboard setup first.',
    '当前账户缺少交易授权。请展开“账户设置”，先检查并设置交易授权。': 'Trading approvals are missing. Open Account settings to check and set approvals first.',
    '请先保存市场配置，再检查交易授权。': 'Save market settings before checking approvals.',
    '请先在账户设置中保存钱包 Private Key。': 'Save the wallet private key in Account settings first.',
    '请先在账户设置中保存钱包 Private Key，才能读取余额。': 'Save the wallet private key in Account settings before reading the balance.',
    '读取余额需要安装 Predict.fun 官方 SDK。': 'The official Predict.fun SDK is required to read the balance.',
    '无法读取链上 USDT 余额，请检查钱包配置和服务器网络。': 'Could not read the on-chain USDT balance. Check wallet settings and the network.',
    '钱包 Private Key 无效，无法生成 JWT。': 'The wallet private key is invalid; a JWT could not be generated.',
    'Privy Wallet Private Key 或 Predict Account Address 无效。': 'Invalid Privy wallet private key or Predict Account Address.',
    '钱包 Private Key 未保存，无法自动重新生成 JWT。': 'No wallet private key saved; the JWT cannot be refreshed automatically.',
    '检查交易授权需要安装 Predict.fun 官方 SDK。': 'The official Predict.fun SDK is required to check approvals.',
    '官方 SDK 未提供签名钱包信息，无法检查授权 Gas 余额。': 'The SDK did not provide signing wallet details, so its gas balance could not be checked.',
    '请先停止机器人，再设置交易授权。': 'Stop the bot before setting approvals.',
    '交易授权已经完整，无需重复设置。': 'Trading approvals are already complete.',
    '交易授权已设置完成。此操作没有创建订单。': 'Trading approvals completed. No orders were placed.',
    '请先停止机器人，再修改配置。': 'Stop the bot before changing quote settings.',
    '市场与风控配置已保存。': 'Market and risk settings saved.',
    '自动生成 JWT 需要 API Key。': 'An API Key is required to generate a JWT.',
    '无法连接 Predict.fun 生成 JWT，请检查 API Key 和网络后重试。': 'Could not connect to Predict.fun to generate a JWT. Check the API Key and network, then retry.',
    '账户设置已保存。': 'Account settings saved.',
    '已使用 Predict Account 的 Privy Wallet 本地签名并自动生成 JWT。': 'JWT generated using a local Privy wallet signature for the Predict account.',
    '已使用 EOA 钱包本地签名并自动生成 JWT。': 'JWT generated using a local EOA wallet signature.',
    '机器人会在下次启动时使用新的账户设置。': 'New account settings will be used the next time the bot starts.',
    '未能读取 Predict.fun 市场页面。请检查网络后重试，或直接填写数字 Market ID。': 'Could not read the Predict.fun market page. Check the network and retry, or enter a numeric Market ID.',
    '已通过 Predict.fun 官方接口读取中文市场名称。请选择要挂单的市场和选项。': 'Chinese market names retrieved from the official Predict.fun API. Choose a market and outcome to quote.',
    '已从中文页面读取市场名称。请选择要挂单的市场和 Yes / No 选项。': 'Market names retrieved from the Chinese page. Choose a market and Yes / No outcome.',
    '官方搜索接口不可用，已从公开页面读取市场。请选择要挂单的市场和选项；实盘前仍需确认 API Key 有效。': 'Official search is unavailable; markets were read from the public page. Choose a market and outcome, and verify your API Key before live trading.',
    '请选择要挂单的市场和选项。': 'Choose a market and outcome to quote.',
    '未找到匹配市场，请尝试直接填写数字 Market ID。': 'No matching market found. Try a numeric Market ID.',
    '机器人正在启动。': 'The bot is starting.',
    '已请求停止机器人，并将执行撤单。': 'Bot stop requested; orders will be canceled.',
    '已请求撤销所有已配置市场的订单。': 'Cancellation requested for all configured markets.',
    '启用有效期时，小时和分钟不能同时为 0。': 'With session duration enabled, hours and minutes cannot both be zero.',
    '请粘贴 predict.fun 的市场网址，例如 https://predict.fun/market/xxx': 'Paste a Predict.fun market URL, such as https://predict.fun/market/xxx',
    '网址中没有找到市场路径，请粘贴完整的 /market/… 链接。': 'No market path found. Paste the full /market/… URL.',
    '网址中没有找到市场名称。': 'No market name found in the URL.',
  };
  const patterns = [
    [/^(\d+) 小时$/, (_, n) => `${n} ${n === '1' ? 'hour' : 'hours'}`],
    [/^(\d+) 分钟$/, (_, n) => `${n} ${n === '1' ? 'minute' : 'minutes'}`],
    [/^市场 (\d+)$/, (_, n) => `Market ${n}`],
    [/^(\d+) 个市场$/, (_, n) => `${n} markets`],
    [/^已配置 (\d+) 个市场$/, (_, n) => `${n} markets configured`],
    [/^每 2 秒刷新 · (\d+) 笔$/, (_, n) => `Updates every 2 seconds · ${n} orders`],
    [/^分布在 (\d+) 个市场$/, (_, n) => `Across ${n} markets`],
    [/^启动后倒计时 (.+)$/, (_, time) => `Countdown after start: ${time}`],
    [/^(\d+)小时 (\d+)分 (\d+)秒$/, (_, h, m, s) => `${h}h ${m}m ${s}s`],
    [/^(\d+)分 (\d+)秒$/, (_, m, s) => `${m}m ${s}s`],
    [/^(\d+)秒$/, (_, s) => `${s}s`],
    [/^已选择[： ](.+)$/, (_, name) => `Selected: ${name.replace(/Yes & No（双向）$/, 'Yes & No (both)')}`],
    [/^选择「(.+)」挂单$/, (_, name) => `Quote ${name === 'Yes & No（双向）' ? 'Yes & No (both)' : name}`],
    [/^(.+) · 订单 (.+)$/, (_, name, id) => `${name === 'Yes & No（双向）' ? 'Yes & No (both)' : name} · Order ${id}`],
    [/^Market ID：(.+)$/, (_, id) => `Market ID: ${id}`],
    [/^缺少 (\d+) 项授权$/, (_, n) => `${n} approvals missing`],
    [/^授权 Gas 余额：(.+)$/, (_, amount) => `Gas balance: ${amount}`],
    [/^服务器错误（(\d+)），请查看运行日志。$/, (_, status) => `Server error (${status}). Check the activity logs.`],
    [/^操作失败（(\d+)），请查看日志。$/, (_, status) => `Action failed (${status}). Check the logs.`],
    [/^机器人停止：([\s\S]+)$/, (_, detail) => `Bot stopped: ${translate(detail)}`],
    [/^Predict.fun 拒绝了 (.+) 请求（HTTP (\d+)）：([\s\S]*)$/, (_, path, code, detail) => `Predict.fun rejected ${path} (HTTP ${code}): ${detail}`],
    [/^市场 (\d+) 尚未识别。.*$/, (_, n) => `Market ${n} has not been identified. Paste its URL, click Identify URL, and select an outcome before saving.`],
    [/^市场 (\d+) 仍是网址。.*$/, (_, n) => `Market ${n} still contains a URL. Click Identify URL and choose the correct market before saving.`],
    [/^市场 (\d+) 尚未选择挂单选项。$/, (_, n) => `Choose an outcome for Market ${n}.`],
    [/^市场 (\d+) 的单次挂单数量必须是有效的正数。$/, (_, n) => `Order size for Market ${n} must be a valid positive number.`],
    [/^(.+)必须是有效的正数。$/, (_, field) => `${translate(field)} must be a valid positive number.`],
    [/^深度保护设置无效：([\s\S]+)$/, (_, detail) => `Invalid depth protection settings: ${detail}`],
    [/^官方 SDK 设置交易授权失败：([\s\S]+)$/, (_, detail) => `The SDK failed to set trading approvals: ${detail}`],
    [/^设置交易授权失败：([\s\S]+)$/, (_, detail) => `Trading approval setup failed: ${detail}`],
    [/^市场 (.+) 缺少官方授权类型信息，已停止以避免错误授权。$/, (_, id) => `Approval metadata is missing for Market ${id}. Stopped to avoid incorrect approvals.`],
  ];
  let language = 'en';

  function translate(source) {
    const value = String(source ?? '');
    if (language === 'zh') return value;
    if (Object.hasOwn(messages, value)) return messages[value];
    for (const [pattern, replacement] of patterns) {
      if (pattern.test(value)) return value.replace(pattern, replacement);
    }
    if (value.startsWith('账户设置已保存。')) {
      return value.split(/(?<=。)\s*/).filter(Boolean).map(translate).join(' ');
    }
    for (const separator of [' · ', ' → ', '；', '：']) {
      if (value.includes(separator)) {
        return value.split(separator).map(translate).join(separator === '；' ? '; ' : separator === '：' ? ': ' : separator);
      }
    }
    return value;
  }

  function setText(element, source) {
    element.dataset.i18nSource = String(source ?? '');
    element.textContent = translate(source);
  }

  function translateStatic(root) {
    const doc = root.ownerDocument || root;
    const walker = doc.createTreeWalker(root, 4);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (node.parentElement?.closest('script, style, [data-i18n-source]')) continue;
      const source = node.textContent.trim();
      if (!Object.hasOwn(messages, source)) continue;
      const parent = node.parentElement;
      if (!parent) continue;
      if (parent.childNodes.length === 1) setText(parent, source);
      else {
        const span = doc.createElement('span');
        setText(span, source);
        node.replaceWith(span);
      }
    }
    for (const element of root.querySelectorAll('[placeholder], [title], [aria-label]')) {
      for (const attr of ['placeholder', 'title', 'aria-label']) {
        const source = element.getAttribute(attr);
        if (!Object.hasOwn(messages, source)) continue;
        element.setAttribute(`data-i18n-${attr}`, source);
        element.setAttribute(attr, translate(source));
      }
    }
  }

  function applyLanguage() {
    const doc = globalThis.document;
    if (!doc) return;
    doc.documentElement.lang = language === 'en' ? 'en' : 'zh-CN';
    for (const element of doc.querySelectorAll('[data-i18n-source]')) {
      element.textContent = translate(element.dataset.i18nSource);
    }
    for (const attr of ['placeholder', 'title', 'aria-label']) {
      for (const element of doc.querySelectorAll(`[data-i18n-${attr}]`)) {
        element.setAttribute(attr, translate(element.getAttribute(`data-i18n-${attr}`)));
      }
    }
    const selector = doc.querySelector('#language-select');
    if (selector) selector.value = language;
    doc.dispatchEvent(new CustomEvent('languagechange', {detail: language}));
  }

  async function setLanguage(value, {persist = true} = {}) {
    if (!['zh', 'en'].includes(value)) return;
    language = value;
    applyLanguage();
    if (!persist) return;
    try { globalThis.localStorage?.setItem('predict-mm-language', language); } catch (_) { /* Optional cache. */ }
  }

  globalThis.PredictI18n = {translate, setText, translateStatic, setLanguage, get language() {return language;}};
  if (!globalThis.document) return;
  translateStatic(document);
  document.documentElement.lang = 'en';
})();
