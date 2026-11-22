<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <h2>监测写入与恢复控制台</h2>
      <p class="sub">化工园区环保监测 · 中心持久接收日志架构</p>
      <el-form @submit.prevent="doLogin">
        <el-form-item>
          <el-input v-model="username" placeholder="用户名（admin / operator）" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" placeholder="密码" show-password />
        </el-form-item>
        <el-button type="primary" style="width: 100%" :loading="loading" @click="doLogin">登录</el-button>
      </el-form>
      <p class="hint">测试账号：admin/admin123，operator/operator123</p>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'

const router = useRouter()
const username = ref('admin')
const password = ref('')
const loading = ref(false)

async function doLogin() {
  loading.value = true
  try {
    const { data } = await api.post('/auth/login', { username: username.value, password: password.value })
    localStorage.setItem('token', data.token)
    localStorage.setItem('username', data.user.username)
    router.push('/')
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap { height: 100vh; display: flex; align-items: center; justify-content: center; background: #0f2a43; }
.login-card { width: 380px; }
h2 { margin: 0 0 4px; font-size: 20px; }
.sub { color: #909399; font-size: 13px; margin: 0 0 20px; }
.hint { color: #909399; font-size: 12px; margin-top: 14px; }
</style>
