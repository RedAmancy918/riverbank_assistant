import SwiftUI

private enum AuthenticationMode {
    case login
    case register
}

struct LoginView: View {
    @EnvironmentObject private var store: AppStore
    @State private var mode: AuthenticationMode = .login
    @State private var server = ""
    @State private var username = ""
    @State private var password = ""
    @State private var passwordConfirmation = ""
    @State private var displayName = ""
    @State private var registrationPasscode = ""
    @State private var bootstrapCredential = ""
    @State private var bootstrapRequired = false
    @State private var registrationEnabled = false
    @State private var initialAdminUsername = ""
    @State private var showsConnectionSettings = false
    @State private var isSubmitting = false
    @State private var message = ""

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 24) {
                    Spacer(minLength: 34)
                    Image("RiverBankMark")
                        .resizable()
                        .scaledToFit()
                        .frame(width: 82, height: 82)
                        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
                        .shadow(color: riverCyan.opacity(0.2), radius: 24)

                    VStack(spacing: 7) {
                        Text(mode == .login ? "登录小灰" : "注册 RiverBank")
                            .font(.largeTitle.bold())
                        Text(
                            mode == .login
                                ? "进入你的独立 Chat 与附件空间"
                                : "注册后自动登录，聊天记录与其他用户隔离"
                        )
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    }

                    authenticationModePicker

                    VStack(spacing: 14) {
                        TextField("用户名", text: $username)
                            .textContentType(.username)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        SecureField("密码", text: $password)
                            .textContentType(mode == .login ? .password : .newPassword)

                        if mode == .register {
                            SecureField("再次输入密码", text: $passwordConfirmation)
                                .textContentType(.newPassword)
                            TextField("显示名称（可选）", text: $displayName)
                                .textContentType(.name)
                            SecureField("注册通行码", text: $registrationPasscode)
                                .textContentType(.oneTimeCode)

                            if !initialAdminUsername.isEmpty {
                                Text("设备尚未建立管理员。请先注册 \(initialAdminUsername) 管理员账号。")
                                    .font(.footnote)
                                    .foregroundStyle(riverCyan.opacity(0.9))
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }

                        DisclosureGroup("连接设置", isExpanded: $showsConnectionSettings) {
                            VStack(alignment: .leading, spacing: 8) {
                                TextField("RiverBank Edge Host", text: $server)
                                    .textContentType(.URL)
                                    .keyboardType(.URL)
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                                Text("仅在更换 Edge 设备或排查连接时需要修改。")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            .padding(.top, 9)
                        }
                        .tint(riverCyan)

                        if mode == .login && bootstrapRequired {
                            Divider().padding(.vertical, 3)
                            Text("首次设置：此账号会成为设备管理员，并接管已有聊天。")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            TextField("显示名称（可选）", text: $displayName)
                                .textContentType(.name)
                            SecureField("一次性设备凭据", text: $bootstrapCredential)
                                .textContentType(.oneTimeCode)
                        }
                    }
                    .textFieldStyle(.plain)
                    .padding(18)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))

                    Button {
                        Task { await submit() }
                    } label: {
                        HStack {
                            if isSubmitting { ProgressView().tint(.black) }
                            Text(primaryButtonTitle)
                                .fontWeight(.semibold)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 14)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.black)
                    .background(riverCyan, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .disabled(isSubmitting)

                    if !message.isEmpty {
                        Text(message)
                            .font(.footnote)
                            .foregroundStyle(.red)
                            .multilineTextAlignment(.center)
                    }
                    Spacer(minLength: 24)
                }
                .padding(.horizontal, 28)
            }
            .background(
                RadialGradient(
                    colors: [riverCyan.opacity(0.15), Color(red: 0.02, green: 0.055, blue: 0.075)],
                    center: .top,
                    startRadius: 20,
                    endRadius: 520
                ).ignoresSafeArea()
            )
            .foregroundStyle(.white)
        }
        .onAppear {
            server = store.server
            username = store.savedUsername
        }
        .onChange(of: server) { _, _ in
            bootstrapRequired = false
            registrationEnabled = false
            initialAdminUsername = ""
        }
    }

    private var authenticationModePicker: some View {
        HStack(spacing: 6) {
            modeButton("登录", value: .login)
            modeButton("注册", value: .register)
        }
        .padding(5)
        .background(.thinMaterial, in: Capsule())
    }

    private func modeButton(_ title: String, value: AuthenticationMode) -> some View {
        Button(title) {
            withAnimation(.easeInOut(duration: 0.2)) { mode = value }
            password = ""
            passwordConfirmation = ""
            registrationPasscode = ""
            bootstrapCredential = ""
            message = ""
            if value == .register {
                Task { await refreshRegistrationConfiguration() }
            }
        }
        .buttonStyle(.plain)
        .font(.subheadline.weight(.semibold))
        .foregroundStyle(mode == value ? Color.black : Color.white.opacity(0.7))
        .frame(maxWidth: .infinity)
        .padding(.vertical, 10)
        .background(mode == value ? riverCyan : Color.clear, in: Capsule())
    }

    private var primaryButtonTitle: String {
        if isSubmitting {
            return mode == .login ? "正在登录…" : "正在注册…"
        }
        return mode == .login ? "登录" : "注册并进入"
    }

    @MainActor
    private func refreshRegistrationConfiguration() async {
        let cleanServer = server.trimmingCharacters(in: .whitespacesAndNewlines)
        guard URL(string: cleanServer)?.scheme?.lowercased() == "https" else { return }
        do {
            let config = try await store.authConfiguration(server: cleanServer)
            registrationEnabled = config.registrationEnabled == true
            initialAdminUsername = config.initialAdminUsername ?? ""
        } catch {
            // Submission shows the actionable connection error.
        }
    }

    @MainActor
    private func submit() async {
        let cleanServer = server.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanUsername = username.trimmingCharacters(in: .whitespacesAndNewlines)
        guard URL(string: cleanServer)?.scheme?.lowercased() == "https" else {
            showsConnectionSettings = true
            message = "账号密码登录必须使用 HTTPS 地址"
            return
        }
        guard cleanUsername.count >= 3 else {
            message = "用户名至少需要 3 个字符"
            return
        }
        guard password.count >= 10 else {
            message = "密码至少需要 10 个字符"
            return
        }
        if mode == .register {
            guard password == passwordConfirmation else {
                message = "两次输入的密码不一致"
                return
            }
            guard !registrationPasscode.isEmpty else {
                message = "请输入注册通行码"
                return
            }
        }

        isSubmitting = true
        defer { isSubmitting = false }
        do {
            let config = try await store.authConfiguration(server: cleanServer)
            registrationEnabled = config.registrationEnabled == true
            initialAdminUsername = config.initialAdminUsername ?? ""

            if mode == .register {
                guard registrationEnabled else {
                    message = "此设备当前未开放账号注册"
                    return
                }
                if !initialAdminUsername.isEmpty,
                   cleanUsername.caseInsensitiveCompare(initialAdminUsername) != .orderedSame {
                    message = "请先注册管理员账号 \(initialAdminUsername)"
                    return
                }
                try await store.signUp(
                    server: cleanServer,
                    username: cleanUsername,
                    password: password,
                    displayName: displayName,
                    registrationPasscode: registrationPasscode
                )
                passwordConfirmation = ""
                registrationPasscode = ""
            } else {
                bootstrapRequired = config.bootstrapRequired
                if bootstrapRequired && bootstrapCredential.count < 16 {
                    message = "首次设置还需要输入一次性设备凭据"
                    return
                }
                try await store.signIn(
                    server: cleanServer,
                    username: cleanUsername,
                    password: password,
                    displayName: displayName,
                    bootstrapCredential: bootstrapRequired ? bootstrapCredential : nil
                )
                bootstrapCredential = ""
            }
            password = ""
            message = ""
        } catch let RiverBankAPIError.server(code, _) where mode == .register && code == 401 {
            message = "注册通行码不正确"
        } catch let RiverBankAPIError.server(code, detail) where mode == .register && code == 409 {
            message = detail.localizedCaseInsensitiveContains("administrator")
                ? "请先注册管理员账号 \(initialAdminUsername.isEmpty ? "Geo" : initialAdminUsername)"
                : "这个用户名已经被使用"
        } catch {
            message = error.localizedDescription
        }
    }
}
