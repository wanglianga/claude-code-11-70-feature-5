import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', component: () => import('./views/Login.vue'), meta: { title: '登录' } },
  { path: '/', component: () => import('./views/Dashboard.vue'), meta: { title: '运行台' } },
  { path: '/logs', component: () => import('./views/Logs.vue'), meta: { title: '接收日志' } },
  { path: '/conclusions', component: () => import('./views/Conclusions.vue'), meta: { title: '质量结论' } },
  { path: '/alerts', component: () => import('./views/Alerts.vue'), meta: { title: '告警' } },
  { path: '/devices', component: () => import('./views/Devices.vue'), meta: { title: '设备目录' } },
  { path: '/policies', component: () => import('./views/Policies.vue'), meta: { title: '策略台账' } },
  { path: '/recovery', component: () => import('./views/Recovery.vue'), meta: { title: '恢复编排' } },
  { path: '/evidence', component: () => import('./views/Evidence.vue'), meta: { title: '证据链' } },
  { path: '/review', component: () => import('./views/Review.vue'), meta: { title: '容量决策复盘' } },
  { path: '/simulate', component: () => import('./views/Simulate.vue'), meta: { title: '场景模拟' } },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  if (to.path !== '/login' && !localStorage.getItem('token')) return '/login'
})

export default router
