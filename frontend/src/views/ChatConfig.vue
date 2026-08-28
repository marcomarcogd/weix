<template>
  <div>
    <h2>聊天配置</h2>
    <el-card>
      <el-form :model="form" label-width="140px">
        <el-form-item label="启用机器人">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-form-item label="群聊权限">
          <el-radio-group v-model="form.group_chat_mode">
            <el-radio label="all">所有人</el-radio>
            <el-radio label="whitelist">仅白名单</el-radio>
            <el-radio label="none">禁用</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="群聊白名单">
          <div style="margin-bottom: 8px">
            <el-tag v-for="room in form.group_whitelist" :key="room" closable @close="removeRoom(room)" style="margin-right: 8px; margin-bottom: 4px">
              {{ roomName(room) }}
            </el-tag>
          </div>
          <el-select
            v-model="selectedRoom"
            filterable
            remote
            :remote-method="filterRooms"
            placeholder="输入关键词搜索群聊"
            style="width: 100%"
            @change="addRoom"
          >
            <el-option
              v-for="room in filteredRooms"
              :key="room.room_id"
              :label="room.name && room.name !== room.room_id ? room.name : room.room_id"
              :value="room.room_id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="私聊权限">
          <el-radio-group v-model="form.private_chat_mode">
            <el-radio label="all">所有人</el-radio>
            <el-radio label="whitelist">仅白名单</el-radio>
            <el-radio label="none">禁用</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="私聊白名单">
          <div style="margin-bottom: 8px">
            <el-tag v-for="user in form.private_whitelist" :key="user" closable @close="removeUser(user)" style="margin-right: 8px; margin-bottom: 4px">
              {{ userName(user) }}
            </el-tag>
          </div>
          <el-select
            v-model="selectedUser"
            filterable
            remote
            :remote-method="filterContacts"
            placeholder="输入关键词搜索用户"
            style="width: 100%"
            @change="addUser"
          >
            <el-option
              v-for="c in filteredContacts"
              :key="c.wxid"
              :label="c.nickname || c.remark || c.alias || c.wxid"
              :value="c.wxid"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="回复模式">
          <el-radio-group v-model="form.reply_mode">
            <el-radio label="keyword">仅关键词</el-radio>
            <el-radio label="ai">AI 自动回复</el-radio>
            <el-radio label="all">全部回复</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-divider content-position="left">Windows 微信 UIA 发送</el-divider>
        <el-alert
          title="自动发送仅使用 UIA 控件定位，不会回退到固定坐标、OCR 或鼠标点击。重名、账号不明或控件不唯一时会保持静默。"
          type="warning"
          :closable="false"
          show-icon
          style="margin-bottom: 18px"
        />
        <el-form-item label="微信账号">
          <div style="width: 100%">
            <el-select
              v-model="selectedAccount"
              placeholder="请选择本机微信账号"
              style="width: 100%"
              :loading="accountsLoading"
              @change="changeAccount"
            >
              <el-option
                v-for="account in accounts"
                :key="account.wxid"
                :label="account.wxid + (account.active ? '（当前绑定）' : '')"
                :value="account.wxid"
              />
            </el-select>
            <div class="hint">
              当前绑定：{{ accountStatus.active || '未绑定' }}
              <span v-if="accountStatus.bound_pid"> / PID {{ accountStatus.bound_pid }}</span>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="发送模式">
          <el-radio-group v-model="form.windows_sender.send_mode">
            <el-radio label="auto">自动（后台优先）</el-radio>
            <el-radio label="background">仅后台</el-radio>
            <el-radio label="foreground">仅前台</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="后台按钮消息">
          <el-switch v-model="form.windows_sender.background_post_message" />
          <span class="inline-hint">仅对 UIA 已确认的发送按钮投递一次</span>
        </el-form-item>
        <el-form-item label="发送前回退前台">
          <el-switch v-model="form.windows_sender.allow_foreground_activation" />
          <span class="inline-hint">只在任何发送动作发生前允许</span>
        </el-form-item>
        <el-form-item label="UIA 状态">
          <div style="width: 100%">
            <el-button :loading="diagnosing" @click="diagnoseUIA">检测微信 UIA</el-button>
            <el-tag v-if="uiaStatus" :type="uiaStatus.available ? 'success' : 'danger'" style="margin-left: 10px">
              {{ uiaStatus.available ? '已就绪' : '不可用' }}
            </el-tag>
            <el-descriptions v-if="uiaStatus" :column="2" border size="small" style="margin-top: 12px">
              <el-descriptions-item label="主窗口">{{ stateText(uiaStatus.main_window) }}</el-descriptions-item>
              <el-descriptions-item label="搜索框">{{ stateText(uiaStatus.search_box) }}</el-descriptions-item>
              <el-descriptions-item label="会话列表">{{ stateText(uiaStatus.session_list) }}</el-descriptions-item>
              <el-descriptions-item label="输入框">{{ stateText(uiaStatus.chat_input) }}</el-descriptions-item>
              <el-descriptions-item label="发送按钮">{{ stateText(uiaStatus.send_button) }}</el-descriptions-item>
              <el-descriptions-item label="原因">{{ uiaStatus.reason || '-' }}</el-descriptions-item>
            </el-descriptions>
            <el-alert
              v-if="uiaStatus?.help"
              :title="uiaStatus.help"
              type="info"
              :closable="false"
              show-icon
              style="margin-top: 10px"
            />
          </div>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="saveConfig" :loading="saving">保存配置</el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'
import {
  getChatConfig,
  updateChatConfig,
  getContacts,
  searchChatrooms,
  searchContactsApi,
  getWeChatAccounts,
  selectWeChatAccount,
  diagnoseWeChatUIA,
} from '../api'
import { ElMessage } from 'element-plus'

const form = reactive<any>({
  enabled: false,
  group_chat_mode: 'whitelist',
  group_whitelist: [],
  group_reply_rules: [],
  private_chat_mode: 'whitelist',
  private_whitelist: [],
  reply_mode: 'all',
  windows_sender: {
    send_mode: 'auto',
    background_post_message: true,
    allow_foreground_activation: true,
  },
})

// 全量数据：用于已选标签的名称展示
const allChatrooms = ref<any[]>([])
const allContacts = ref<any[]>([])

// 下拉选项：仅展示搜索结果（默认空，避免渲染数千 DOM）
const filteredRooms = ref<any[]>([])
const filteredContacts = ref<any[]>([])

const selectedRoom = ref('')
const selectedUser = ref('')
const saving = ref(false)
const accounts = ref<any[]>([])
const accountsLoading = ref(false)
const selectedAccount = ref('')
const accountStatus = reactive<any>({ active: '', bound_pid: null })
const diagnosing = ref(false)
const uiaStatus = ref<any>(null)

function stateText(value: boolean) {
  return value ? '已找到' : '未找到'
}

async function loadAccounts() {
  accountsLoading.value = true
  try {
    const res = await getWeChatAccounts()
    accounts.value = res.data?.accounts || []
    selectedAccount.value = res.data?.selected || ''
    accountStatus.active = res.data?.active || ''
    accountStatus.bound_pid = res.data?.bound_pid || null
  } finally {
    accountsLoading.value = false
  }
}

async function changeAccount(wxid: string) {
  if (!wxid) return
  await selectWeChatAccount(wxid)
  uiaStatus.value = null
  accountStatus.active = ''
  accountStatus.bound_pid = null
  ElMessage.warning('账号已保存。请使用 WeixManager 重启服务，重启前自动发送保持静默。')
}

async function diagnoseUIA() {
  diagnosing.value = true
  try {
    const res = await diagnoseWeChatUIA()
    uiaStatus.value = res.data
    if (res.data?.available) {
      ElMessage.success('微信 UIA 关键控件已就绪')
    } else {
      ElMessage.warning(res.data?.reason || '微信 UIA 不可用')
    }
  } finally {
    diagnosing.value = false
  }
}

function roomName(id: string) {
  const found = allChatrooms.value.find((r: any) => r.room_id === id)
  if (!found) return id
  return found.name && found.name !== found.room_id ? found.name : found.room_id
}

function userName(id: string) {
  const found = allContacts.value.find((c: any) => c.wxid === id)
  if (!found) return id
  return found.nickname || found.remark || found.alias || id
}

function matchRoom(keyword: string, room: any) {
  const kw = keyword.toLowerCase()
  return (room.name || '').toLowerCase().includes(kw) ||
    (room.room_id || '').toLowerCase().includes(kw)
}

function matchContact(keyword: string, c: any) {
  const kw = keyword.toLowerCase()
  return (c.nickname || '').toLowerCase().includes(kw) ||
    (c.remark || '').toLowerCase().includes(kw) ||
    (c.alias || '').toLowerCase().includes(kw) ||
    (c.wxid || '').toLowerCase().includes(kw)
}

let searchTimer: ReturnType<typeof setTimeout> | null = null

function filterRooms(keyword: string) {
  if (!keyword) { filteredRooms.value = []; return }
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(async () => {
    try {
      const res = await searchChatrooms(keyword)
      filteredRooms.value = (res.data?.chatrooms || []).slice(0, 50)
    } catch {
      filteredRooms.value = allChatrooms.value
        .filter((r: any) => matchRoom(keyword, r))
        .slice(0, 50)
    }
  }, 300)
}

function filterContacts(keyword: string) {
  if (!keyword) { filteredContacts.value = []; return }
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(async () => {
    try {
      const res = await searchContactsApi(keyword)
      filteredContacts.value = (res.data?.contacts || []).slice(0, 50)
    } catch {
      filteredContacts.value = allContacts.value
        .filter((c: any) => matchContact(keyword, c))
        .slice(0, 50)
    }
  }, 300)
}

function addRoom(roomId: string) {
  if (roomId && !form.group_whitelist.includes(roomId)) {
    form.group_whitelist.push(roomId)
  }
  selectedRoom.value = ''
  filteredRooms.value = []
}

function removeRoom(room: string) {
  form.group_whitelist = form.group_whitelist.filter((r: string) => r !== room)
  const rules = Array.isArray(form.group_reply_rules) ? form.group_reply_rules : []
  const remainingRules = rules.filter((rule: any) => rule?.room_id !== room)
  if (remainingRules.length !== rules.length) {
    form.group_reply_rules = remainingRules
    ElMessage.info('已同步移除该群的群聊专属规则，保存后生效')
  }
}

function addUser(wxid: string) {
  if (wxid && !form.private_whitelist.includes(wxid)) {
    form.private_whitelist.push(wxid)
  }
  selectedUser.value = ''
  filteredContacts.value = []
}

function removeUser(user: string) {
  form.private_whitelist = form.private_whitelist.filter((u: string) => u !== user)
}

onMounted(async () => {
  try {
    const res = await getChatConfig()
    if (res.data) Object.assign(form, res.data)
  } catch {
    ElMessage.error('加载配置失败')
  }

  try {
    const res = await getContacts('all')
    if (res.data.error) {
      ElMessage.warning(res.data.error)
    }
    allChatrooms.value = res.data.chatrooms || []
    allContacts.value = res.data.contacts || []
  } catch {
    ElMessage.error('加载联系人列表失败，请确认后端服务已启动')
  }

  try {
    await loadAccounts()
  } catch {
    ElMessage.error('加载微信账号列表失败')
  }

  if (window.location.hash.includes('diagnose=1')) {
    await diagnoseUIA()
  }
})

async function saveConfig() {
  saving.value = true
  try {
    await updateChatConfig(form)
    ElMessage.success('配置已保存')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.hint {
  margin-top: 6px;
  color: #909399;
  font-size: 12px;
}

.inline-hint {
  margin-left: 10px;
  color: #909399;
  font-size: 12px;
}
</style>
