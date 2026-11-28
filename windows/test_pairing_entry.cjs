const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'desktop.js'), 'utf8');
const startPairing = source.slice(source.indexOf('async function startPairing()'),
  source.indexOf("document.querySelectorAll('[data-add-device]').forEach"));

function setup(authorize, nativeState) {
  const actions = [];
  const button = {disabled: false};
  const context = vm.createContext({
    window: {pywebview: {api: {}}},
    document: {querySelectorAll: () => [button]},
    pairingAuthorization: false,
    lanPairError: '',
    pairDialogGeneration: 1,
    preparePairDialog: message => {
      actions.push(message);
      return 1;
    },
    showPairRetry: message => actions.push(message),
    nativeState,
    receiverState: {},
    byId: () => ({open: false}),
    renderReceiverState: () => {},
    queueRefresh: () => {},
    bridge: async method => {
      actions.push(method);
      return authorize();
    },
    pair: async () => actions.push('qr'),
  });
  vm.runInContext(startPairing, context);
  return {context, actions, button};
}

test('first click waits for authorization, then opens the QR once', async () => {
  let approve;
  const {context, actions, button} = setup(() => new Promise(resolve => { approve = resolve; }),
    {running: false});
  const pending = context.startPairing();
  assert.equal(button.disabled, true);
  await context.startPairing();
  assert.deepEqual(actions, ['正在检查局域网和 Windows 授权…', 'authorize_firewall']);
  approve({serviceReady: true});
  await pending;
  assert.deepEqual(actions, ['正在检查局域网和 Windows 授权…', 'authorize_firewall', 'qr']);
  assert.equal(button.disabled, false);
});

test('cancelling displays the reason and leaves the button available', async () => {
  const {context, actions, button} = setup(() => ({serviceReady: false, message: '已取消 Windows 授权。'}),
    {running: false});
  await context.startPairing();
  assert.deepEqual(actions, ['正在检查局域网和 Windows 授权…', 'authorize_firewall', '已取消 Windows 授权。']);
  assert.equal(context.lanPairError, '已取消 Windows 授权。');
  assert.equal(button.disabled, false);
});

test('an already authorized listener goes straight to the QR', async () => {
  const {context, actions} = setup(() => assert.fail('unexpected prompt'),
    {running: true, firewall: {authorized: true}});
  await context.startPairing();
  assert.deepEqual(actions, ['qr']);
});
