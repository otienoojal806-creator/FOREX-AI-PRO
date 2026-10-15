import { Navigate, Route, Routes } from 'react-router-dom'
import { RequireAdmin, RequireAuth, RequireModule } from './auth/guards'
import Layout from './components/Layout'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Billing from './pages/Billing'
import Transactions from './pages/Transactions'
import Account from './pages/Account'
import AIAnalysis from './pages/AIAnalysis'
import Automation from './pages/Automation'
import Admin from './pages/admin/Admin'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="billing" element={<Billing />} />
          <Route path="modules" element={<Navigate to="/billing" replace />} />
          <Route path="transactions" element={<Transactions />} />
          <Route path="settings" element={<Account />} />
          <Route element={<RequireModule module="ai_analysis" />}>
            <Route path="analysis" element={<AIAnalysis />} />
          </Route>
          <Route element={<RequireModule module="auto_trading" />}>
            <Route path="automation" element={<Automation />} />
          </Route>
          <Route element={<RequireAdmin />}>
            <Route path="admin" element={<Admin />} />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
