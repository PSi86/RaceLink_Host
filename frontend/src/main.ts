import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import { router } from './router'
// Order matters: tailwind.css declares the design tokens (color/spacing
// vars under @theme) that racelink.css's rules consume via
// var(--color-bg) etc. Importing tailwind first guarantees the
// variables resolve. The tailwind import skips Preflight to keep the
// visual diff at zero (see styles/tailwind.css).
import './styles/tailwind.css'
import './styles/racelink.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
