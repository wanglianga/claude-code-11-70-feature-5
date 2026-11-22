<template>
  <router-view v-if="$route.path === '/login'" />
  <el-container v-else class="layout">
    <el-aside width="220px">
      <div class="brand">监测写入与恢复控制台</div>
      <el-menu :default-active="$route.path" router background-color="#0f2a43" text-color="#b8c7d6"
        active-text-color="#ffffff">
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">{{ m.title }}</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="topbar">
        <span class="page-title">{{ $route.meta.title }}</span>
        <div class="user">
          <span>{{ username }}</span>
          <el-button size="small" @click="logout">退出</el-button>
        </div>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const menus = [
  { path: '/', title: '运行台' },
  { path: '/logs', title: '接收日志' },
  { path: '/conclusions', title: '质量结论' },
  { path: '/alerts', title: '告警' },
  { path: '/devices', title: '设备目录' },
  { path: '/policies', title: '策略台账' },
  { path: '/recovery', title: '恢复编排' },
  { path: '/evidence', title: '证据链' },
  { path: '/review', title: '容量决策复盘' },
  { path: '/simulate', title: '场景模拟' },
]
const username = computed(() => localStorage.getItem('username') || '')
function logout() {
  localStorage.removeItem('token')
  localStorage.removeItem('username')
  router.push('/login')
}
</script>

<style>
body { margin: 0; font-family: 'Helvetica Neue', Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif; }
.layout { height: 100vh; }
.el-aside { background: #0f2a43; }
.brand { color: #fff; font-weight: 600; padding: 18px 16px; font-size: 15px; line-height: 1.4; }
.el-menu { border-right: none; }
.topbar { display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #e4e7ed; background: #fff; }
.page-title { font-size: 16px; font-weight: 600; }
.user { display: flex; align-items: center; gap: 10px; color: #606266; }
.el-main { background: #f2f5f9; }
</style>
