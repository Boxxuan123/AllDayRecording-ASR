export const $ = (selector) => document.querySelector(selector)
export const $$ = (selector) => Array.from(document.querySelectorAll(selector))

export function node(tag, className, text) {
  const element = document.createElement(tag)
  if (className) element.className = className
  if (text !== undefined && text !== null) element.textContent = String(text)
  return element
}

export function toast(message, type = 'success') {
  const item = node('div', `toast ${type === 'error' ? 'error' : ''}`, message)
  $('#toast-region').append(item)
  window.setTimeout(() => item.remove(), 3600)
}
