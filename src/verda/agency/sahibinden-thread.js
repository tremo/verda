/* Static DOM reader for one open conversation. No network, clicks, typing, hidden fields, or model-written code. */
(() => {
  const visible = node => !!node && node.getClientRects().length > 0 && node.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const bubbles = [...document.querySelectorAll('.message-detail-panel .message-bubble-wrapper')].filter(visible);
  const messages = bubbles.map(wrapper => {
    const bubble = wrapper.querySelector('.message-bubble');
    const text = wrapper.querySelector('.message-text');
    const time = wrapper.querySelector('.message-time');
    return {
      role: bubble?.classList.contains('sent') ? 'user' : bubble?.classList.contains('received') ? 'seller' : null,
      text: text ? text.innerText.trim() : '',
      sent_at_text: time ? time.innerText.trim() : null,
    };
  });
  const inbox = [...document.querySelectorAll('.message-card-by-person')].filter(visible).length;
  return {url: location.href, panel: !!document.querySelector('.message-detail-panel .message-conversation'), inbox_rows: inbox, messages};
})()
