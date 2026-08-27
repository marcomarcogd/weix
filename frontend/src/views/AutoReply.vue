<template>
  <div>
    <h2>自动回复规则</h2>

    <el-card class="group-rule-card">
      <template #header>
        <div class="card-header">
          <div>
            <div class="card-title">群聊专属规则</div>
            <div class="card-subtitle">每个群可单独选择即时回复或等待 20 秒合并；未命中时保持静默。</div>
          </div>
          <el-button type="primary" :disabled="groupWhitelist.length === 0" @click="showGroupDialog()">
            新增群聊专属规则
          </el-button>
        </div>
      </template>

      <el-alert
        v-if="groupWhitelist.length === 0"
        title="请先在“聊天配置”中把目标群加入群聊白名单"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
      />

      <el-table :data="groupRules" stripe empty-text="暂无群聊专属规则">
        <el-table-column label="目标群" min-width="180">
          <template #default="{ row }">{{ groupName(row.room_id) }}</template>
        </el-table-column>
        <el-table-column prop="keyword" label="包含关键词" min-width="150" />
        <el-table-column prop="reply" label="回复内容" min-width="140" show-overflow-tooltip />
        <el-table-column label="回复时机" width="110">
          <template #default="{ row }">
            <el-tag :type="row.immediate ? 'success' : 'info'">
              {{ row.immediate ? '立即回复' : '20 秒合并' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="未命中" width="120">
          <template #default><el-tag type="info">保持静默</el-tag></template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row, $index }">
            <el-switch :model-value="row.enabled" @change="toggleGroupRule(row, $index)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row, $index }">
            <el-button size="small" @click="showGroupDialog(row, $index)">编辑</el-button>
            <el-button size="small" type="danger" @click="deleteGroupRule($index)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <h3>全局规则</h3>
    <el-button type="primary" @click="showDialog()" style="margin-bottom: 16px">新增规则</el-button>
    <el-table :data="rules" stripe>
      <el-table-column prop="name" label="规则名称" width="150" />
      <el-table-column prop="type" label="类型" width="100">
        <template #default="{ row }">
          <el-tag size="small">{{ ruleTypeLabels[row.type as RuleType] || row.type }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="patterns" label="匹配模式">
        <template #default="{ row }">{{ (row.patterns || []).join(', ') }}</template>
      </el-table-column>
      <el-table-column prop="reply" label="回复内容" show-overflow-tooltip />
      <el-table-column prop="priority" label="优先级" width="80" />
      <el-table-column prop="enabled" label="状态" width="80">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" @change="toggleRule(row)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150">
        <template #default="{ row }">
          <el-button size="small" @click="showDialog(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="handleDelete(row.id)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog :title="editing?.id ? '编辑规则' : '新增规则'" v-model="dialogVisible" width="600px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="规则名称">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="form.type">
            <el-option label="关键词" value="keyword" />
            <el-option label="正则" value="regex" />
            <el-option label="意图" value="intent" />
          </el-select>
        </el-form-item>
        <el-form-item label="匹配模式">
          <el-tag v-for="(p, i) in form.patterns" :key="i" closable @close="form.patterns.splice(i, 1)" style="margin-right: 8px">{{ p }}</el-tag>
          <el-input v-model="newPattern" placeholder="输入模式" style="width: 150px" @keyup.enter="addPattern" />
        </el-form-item>
        <el-form-item label="回复内容">
          <el-input v-model="form.reply" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="触发工作流" v-if="form.type === 'intent'">
          <el-input v-model="form.workflow" placeholder="工作流名称" />
        </el-form-item>
        <el-form-item label="优先级">
          <el-input-number v-model="form.priority" :min="0" :max="100" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog :title="editingGroupIndex >= 0 ? '编辑群聊专属规则' : '新增群聊专属规则'" v-model="groupDialogVisible" width="560px">
      <el-form :model="groupForm" label-width="110px">
        <el-form-item label="目标群" required>
          <el-select v-model="groupForm.room_id" filterable placeholder="请选择群聊白名单中的群" style="width: 100%">
            <el-option
              v-for="roomId in groupWhitelist"
              :key="roomId"
              :label="groupName(roomId)"
              :value="roomId"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="匹配方式">
          <el-input model-value="消息任意位置包含关键词" disabled />
        </el-form-item>
        <el-form-item label="包含关键词" required>
          <el-input v-model="groupForm.keyword" placeholder="例如：@所有人" maxlength="100" show-word-limit />
        </el-form-item>
        <el-form-item label="回复内容" required>
          <el-input v-model="groupForm.reply" type="textarea" :rows="3" placeholder="例如：1（可自定义）" maxlength="500" show-word-limit />
        </el-form-item>
        <el-form-item label="立即回复">
          <el-switch v-model="groupForm.immediate" />
          <span class="field-tip">
            {{ groupForm.immediate ? '检测到关键词后马上发送，不等待消息合并' : '等待 20 秒，合并该群消息后再匹配' }}
          </span>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="groupForm.enabled" />
        </el-form-item>
        <el-alert
          title="该群只执行这条专属规则；未命中时不会调用 AI 或其他全局规则。每条规则可独立设置回复时机。"
          type="info"
          :closable="false"
          show-icon
        />
      </el-form>
      <template #footer>
        <el-button @click="groupDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingGroupRule" @click="saveGroupRule">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import {
  getRules,
  createRule,
  updateRule,
  deleteRule,
  getChatConfig,
  updateChatConfig,
  getContacts,
} from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface GroupReplyRule {
  room_id: string
  match_type: 'contains'
  keyword: string
  reply: string
  rule_only: boolean
  immediate: boolean
  enabled: boolean
}

const rules = ref<any[]>([])
const dialogVisible = ref(false)
const editing = ref<any>(null)
const newPattern = ref('')
const form = reactive<any>({ name: '', type: 'keyword', patterns: [], reply: '', workflow: '', priority: 0, enabled: true })
const groupRules = ref<GroupReplyRule[]>([])
const groupWhitelist = ref<string[]>([])
const chatrooms = ref<any[]>([])
const groupDialogVisible = ref(false)
const editingGroupIndex = ref(-1)
const savingGroupRule = ref(false)
const groupForm = reactive<GroupReplyRule>({
  room_id: '',
  match_type: 'contains',
  keyword: '@所有人',
  reply: '1',
  rule_only: true,
  immediate: true,
  enabled: true,
})
type RuleType = 'keyword' | 'regex' | 'intent'
const ruleTypeLabels: Record<RuleType, string> = {
  keyword: '关键词',
  regex: '正则',
  intent: '意图',
}

function showDialog(row?: any) {
  editing.value = row || null
  if (row) {
    Object.assign(form, { ...row })
  } else {
    Object.assign(form, { name: '', type: 'keyword', patterns: [], reply: '', workflow: '', priority: 0, enabled: true })
  }
  dialogVisible.value = true
}

function addPattern() {
  if (newPattern.value) {
    form.patterns.push(newPattern.value)
    newPattern.value = ''
  }
}

async function loadRules() {
  const res = await getRules()
  rules.value = res.data
}

function groupName(roomId: string) {
  const found = chatrooms.value.find((room: any) => room.room_id === roomId)
  if (!found) return roomId
  return found.name && found.name !== roomId ? found.name : roomId
}

async function loadGroupRules() {
  const [configRes, contactsRes] = await Promise.all([
    getChatConfig(),
    getContacts('chatrooms'),
  ])
  groupWhitelist.value = configRes.data?.group_whitelist || []
  groupRules.value = (configRes.data?.group_reply_rules || []).map((rule: any) => ({
    room_id: String(rule.room_id || ''),
    match_type: 'contains',
    keyword: String(rule.keyword || ''),
    reply: String(rule.reply || ''),
    rule_only: rule.rule_only !== false,
    immediate: rule.immediate !== false,
    enabled: rule.enabled !== false,
  }))
  chatrooms.value = contactsRes.data?.chatrooms || []
  if (contactsRes.data?.error) {
    ElMessage.warning(contactsRes.data.error)
  }
}

function showGroupDialog(row?: GroupReplyRule, index: number = -1) {
  editingGroupIndex.value = index
  Object.assign(groupForm, row || {
    room_id: '',
    match_type: 'contains',
    keyword: '@所有人',
    reply: '1',
    rule_only: true,
    immediate: true,
    enabled: true,
  })
  // 该功能的目标是专属群只执行固定规则，不允许在此处打开 AI 兜底。
  groupForm.match_type = 'contains'
  groupForm.rule_only = true
  groupDialogVisible.value = true
}

async function persistGroupRules(nextRules: GroupReplyRule[]) {
  await updateChatConfig({ group_reply_rules: nextRules })
  groupRules.value = nextRules
}

async function saveGroupRule() {
  const roomId = groupForm.room_id.trim()
  const keyword = groupForm.keyword.trim()
  const reply = groupForm.reply.trim()
  if (!roomId) return ElMessage.warning('请选择目标群')
  if (!keyword) return ElMessage.warning('包含关键词不能为空')
  if (!reply) return ElMessage.warning('回复内容不能为空')

  const duplicate = groupRules.value.some((rule, index) =>
    index !== editingGroupIndex.value &&
    rule.room_id === roomId &&
    rule.enabled &&
    groupForm.enabled,
  )
  if (duplicate) return ElMessage.warning('同一群只能启用一条专属规则')

  const normalized: GroupReplyRule = {
    room_id: roomId,
    match_type: 'contains',
    keyword,
    reply,
    rule_only: true,
    immediate: groupForm.immediate,
    enabled: groupForm.enabled,
  }
  const nextRules = [...groupRules.value]
  if (editingGroupIndex.value >= 0) {
    nextRules[editingGroupIndex.value] = normalized
  } else {
    nextRules.push(normalized)
  }

  savingGroupRule.value = true
  try {
    await persistGroupRules(nextRules)
    groupDialogVisible.value = false
    ElMessage.success('群聊专属规则已保存并立即生效')
  } finally {
    savingGroupRule.value = false
  }
}

async function toggleGroupRule(row: GroupReplyRule, index: number) {
  const nextRules = groupRules.value.map((rule, current) =>
    current === index ? { ...rule, enabled: !row.enabled } : rule,
  )
  await persistGroupRules(nextRules)
  ElMessage.success(row.enabled ? '规则已停用' : '规则已启用')
}

async function deleteGroupRule(index: number) {
  await ElMessageBox.confirm('确定删除这条群聊专属规则?', '提示', { type: 'warning' })
  await persistGroupRules(groupRules.value.filter((_, current) => current !== index))
  ElMessage.success('群聊专属规则已删除')
}

async function handleSave() {
  if (editing.value?.id) {
    await updateRule(editing.value.id, form)
  } else {
    await createRule(form)
  }
  dialogVisible.value = false
  ElMessage.success('保存成功')
  await loadRules()
}

async function handleDelete(id: number) {
  await ElMessageBox.confirm('确定删除此规则?', '提示', { type: 'warning' })
  await deleteRule(id)
  ElMessage.success('删除成功')
  await loadRules()
}

async function toggleRule(row: any) {
  await updateRule(row.id, { enabled: !row.enabled })
  await loadRules()
}

onMounted(async () => {
  await Promise.all([loadRules(), loadGroupRules()])
})
</script>

<style scoped>
.group-rule-card {
  margin-bottom: 20px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.card-title {
  font-size: 16px;
  font-weight: 600;
}

.card-subtitle {
  margin-top: 4px;
  color: #909399;
  font-size: 13px;
}

.field-tip {
  margin-left: 12px;
  color: #909399;
  font-size: 13px;
}
</style>
