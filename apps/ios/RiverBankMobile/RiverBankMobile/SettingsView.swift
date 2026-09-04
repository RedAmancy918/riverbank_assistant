import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var store: AppStore
    @State private var testing = false
    @State private var currentPassword = ""
    @State private var newPassword = ""
    @State private var passwordMessage = ""
    @State private var changingPassword = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(spacing: 16) {
                        Image("RiverBankMark")
                            .resizable()
                            .scaledToFit()
                            .frame(width: 72, height: 72)
                            .clipShape(RoundedRectangle(cornerRadius: 18))
                        VStack(alignment: .leading, spacing: 5) {
                            Text("RiverBank iOS 客户端")
                                .font(.title3.bold())
                            Text(appDisplayVersion)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                            Text("灰度流动科技有限公司")
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                        }
                    }
                    .padding(.vertical, 6)
                }

                Section("账号") {
                    LabeledContent("显示名称", value: store.currentUser?.displayName ?? "—")
                    LabeledContent("用户名", value: store.currentUser.map { "@\($0.username)" } ?? "—")
                    LabeledContent("权限", value: store.currentUser?.role == "admin" ? "管理员" : "普通用户")
                    Button("安全登出", role: .destructive) {
                        Task { await store.logout() }
                    }
                }

                Section("修改密码") {
                    SecureField("当前密码", text: $currentPassword)
                        .textContentType(.password)
                    SecureField("新密码（至少 10 个字符）", text: $newPassword)
                        .textContentType(.newPassword)
                    Button(changingPassword ? "正在更新…" : "更新密码") {
                        Task { await updatePassword() }
                    }
                    .disabled(changingPassword || currentPassword.isEmpty || newPassword.count < 10)
                    if !passwordMessage.isEmpty {
                        Text(passwordMessage).font(.footnote).foregroundStyle(.secondary)
                    }
                }

                Section("RiverBank Edge") {
                    LabeledContent("Host") {
                        Text(store.server)
                            .font(.caption.monospaced())
                            .multilineTextAlignment(.trailing)
                    }
                    Button(testing ? "检测中…" : "测试连接") {
                        Task {
                            testing = true
                            await store.testConnection()
                            testing = false
                        }
                    }
                    .disabled(testing)
                    LabeledContent("兼容的 Edge OS", value: compatibleEdgeRange)
                }

                Section("状态") {
                    LabeledContent("连接", value: store.connectionMessage)
                    LabeledContent("任务") { Text("\(store.tasks.count)") }
                    LabeledContent("报告") { Text("\(store.reports.count)") }
                }

                Section {
                    Text("账号会话保存在本机钥匙串中，密码不会保存。Chat 历史与附件由服务端按用户隔离；任务和设备报告目前仍是设备级共享资源。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("设置")
        }
    }

    private var appDisplayVersion: String {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
            ?? "0.0.0"
        let channel = Bundle.main.object(forInfoDictionaryKey: "RiverBankReleaseChannel") as? String
            ?? "beta"
        return "v\(version) \(channel)"
    }

    private var compatibleEdgeRange: String {
        Bundle.main.object(forInfoDictionaryKey: "RiverBankCompatibleEdgeRange") as? String
            ?? "未声明"
    }

    @MainActor
    private func updatePassword() async {
        changingPassword = true
        defer { changingPassword = false }
        do {
            try await store.changePassword(current: currentPassword, new: newPassword)
            currentPassword = ""
            newPassword = ""
            passwordMessage = "密码已更新，其他设备上的登录会话已撤销"
        } catch {
            passwordMessage = error.localizedDescription
        }
    }
}
