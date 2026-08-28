import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var store: AppStore
    @State private var server = ""
    @State private var token = ""
    @State private var testing = false

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
                            Text("RiverBank Assistant")
                                .font(.title3.bold())
                            Text("v0.20.0 beta")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                            Text("灰度流动科技有限公司")
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                        }
                    }
                    .padding(.vertical, 6)
                }

                Section("树莓派") {
                    TextField("HTTPS 地址", text: $server)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                    SecureField("配对令牌", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button("保存") {
                        store.saveSettings(server: server, token: token)
                    }
                    Button(testing ? "检测中…" : "测试连接") {
                        Task {
                            testing = true
                            store.saveSettings(server: server, token: token)
                            await store.testConnection()
                            testing = false
                        }
                    }
                    .disabled(testing)
                }

                Section("状态") {
                    LabeledContent("连接", value: store.connectionMessage)
                    LabeledContent("任务") { Text("\(store.tasks.count)") }
                    LabeledContent("报告") { Text("\(store.reports.count)") }
                }

                Section {
                    Text("任务提交后由树莓派独立执行。退出 App、切换网络或锁屏不会中断任务；再次打开即可继续查看、补充信息或下载报告。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("设置")
            .onAppear {
                server = store.server
                token = store.token
            }
        }
    }
}
