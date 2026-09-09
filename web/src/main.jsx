import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { initBase } from './api'
import './index.css'

// In the packaged app the shell picks a free port for the backend, so the
// API base has to be resolved before the first request goes out.
initBase().finally(() => {
  ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode><App /></React.StrictMode>
  )
})
