import Foundation
import UIKit

@MainActor
final class AppStore: ObservableObject {
    @Published var tasks: [RemoteTask] = []
    @Published var reports: [ReportRecord] = []
    @Published var chats: [ChatConversation] = []
    @Published var chatMessages: [ChatMessage] = []
    @Published var activeChatID: String?
    @Published var server: String
    @Published var token: String
    @Published var currentUser: AuthUser?
    @Published var isAuthenticated = false
    @Published var isRestoringSession = true
    @Published var errorMessage = ""
    @Published var connectionMessage = "尚未检测"
    @Published var isLoading = false

    private let serverKey = "riverbank.server"
    private let tokenAccount = "session-token-v1"
    private let usernameKey = "riverbank.username"

    init() {
        server = UserDefaults.standard.string(forKey: serverKey)
            ?? "https://riverbank-tech.tail0acdab.ts.net/assistant"
        token = KeychainStore.read(tokenAccount)
        activeChatID = nil
        if !token.hasPrefix("rbs_") {
            token = ""
            KeychainStore.write("", account: tokenAccount)
        }
    }

    private var client: APIClient {
        APIClient(server: server, token: token)
    }

    private var activeChatKey: String? {
        currentUser.map { "riverbank.active-chat.\($0.id)" }
    }

    private func normalizedServer(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    func authConfiguration(server: String) async throws -> AuthConfigEnvelope {
        try await APIClient(server: normalizedServer(server), token: "").authConfig()
    }

    func signIn(
        server: String,
        username: String,
        password: String,
        displayName: String = "",
        bootstrapCredential: String? = nil
    ) async throws {
        let cleanServer = normalizedServer(server)
        let unauthenticated = APIClient(server: cleanServer, token: "")
        let session: AuthSessionEnvelope
        if let bootstrapCredential {
            session = try await unauthenticated.bootstrap(
                username: username,
                password: password,
                displayName: displayName,
                deviceName: UIDevice.current.name,
                credential: bootstrapCredential
            )
        } else {
            session = try await unauthenticated.login(
                username: username,
                password: password,
                deviceName: UIDevice.current.name
            )
        }
        self.server = cleanServer
        token = session.token
        currentUser = session.user
        isAuthenticated = true
        UserDefaults.standard.set(cleanServer, forKey: serverKey)
        UserDefaults.standard.set(session.user.username, forKey: usernameKey)
        KeychainStore.write(session.token, account: tokenAccount)
        activeChatID = activeChatKey.flatMap { UserDefaults.standard.string(forKey: $0) }
        connectionMessage = "已连接 RiverBank"
        errorMessage = ""
    }

    func signUp(
        server: String,
        username: String,
        password: String,
        displayName: String,
        registrationPasscode: String
    ) async throws {
        let cleanServer = normalizedServer(server)
        let session = try await APIClient(
            server: cleanServer,
            token: ""
        ).register(
            username: username,
            password: password,
            displayName: displayName,
            registrationPasscode: registrationPasscode,
            deviceName: UIDevice.current.name
        )
        self.server = cleanServer
        token = session.token
        currentUser = session.user
        isAuthenticated = true
        UserDefaults.standard.set(cleanServer, forKey: serverKey)
        UserDefaults.standard.set(session.user.username, forKey: usernameKey)
        KeychainStore.write(session.token, account: tokenAccount)
        activeChatID = nil
        connectionMessage = "已连接 RiverBank"
        errorMessage = ""
    }

    func restoreSession() async {
        guard isRestoringSession else { return }
        defer { isRestoringSession = false }
        guard token.hasPrefix("rbs_") else { return }
        do {
            let account = try await client.currentUser().user
            currentUser = account
            isAuthenticated = true
            activeChatID = activeChatKey.flatMap { UserDefaults.standard.string(forKey: $0) }
            connectionMessage = "已连接 RiverBank"
        } catch {
            token = ""
            currentUser = nil
            isAuthenticated = false
            KeychainStore.write("", account: tokenAccount)
        }
    }

    func logout() async {
        if !token.isEmpty { try? await client.logout() }
        token = ""
        currentUser = nil
        isAuthenticated = false
        activeChatID = nil
        chats = []
        chatMessages = []
        tasks = []
        reports = []
        KeychainStore.write("", account: tokenAccount)
        connectionMessage = "已安全登出"
    }

    func changePassword(current: String, new: String) async throws {
        try await client.changePassword(current: current, new: new)
    }

    func chatAttachmentData(_ attachment: ChatAttachment) async throws -> Data {
        try await client.chatAttachment(attachment)
    }

    var savedUsername: String {
        UserDefaults.standard.string(forKey: usernameKey) ?? ""
    }

    func testConnection() async {
        do {
            _ = try await client.status()
            connectionMessage = "已连接 RiverBank"
            errorMessage = ""
        } catch {
            guard !error.isRiverBankCancellation else { return }
            connectionMessage = "连接失败"
            errorMessage = error.localizedDescription
        }
    }

    func refreshTasks(showLoading: Bool = false) async {
        if showLoading { isLoading = true }
        defer { if showLoading { isLoading = false } }
        do {
            tasks = try await client.listTasks().tasks
            errorMessage = ""
        } catch {
            guard !error.isRiverBankCancellation else { return }
            errorMessage = error.localizedDescription
        }
    }

    func refreshReports(showLoading: Bool = false) async {
        if showLoading { isLoading = true }
        defer { if showLoading { isLoading = false } }
        do {
            reports = try await client.listReports().reports
            errorMessage = ""
        } catch {
            guard !error.isRiverBankCancellation else { return }
            errorMessage = error.localizedDescription
        }
    }

    func createTask(
        title: String,
        prompt: String,
        kind: String,
        outputFormat: String
    ) async throws -> RemoteTask {
        let task = try await client.createTask(
            title: title,
            prompt: prompt,
            kind: kind,
            outputFormat: outputFormat
        )
        tasks.insert(task, at: 0)
        return task
    }

    func fetchTask(id: String) async throws -> RemoteTask {
        let task = try await client.task(id: id)
        if let index = tasks.firstIndex(where: { $0.id == id }) {
            tasks[index] = task
        } else {
            tasks.insert(task, at: 0)
        }
        return task
    }

    func answer(taskID: String, answer: String) async throws -> RemoteTask {
        let task = try await client.answer(taskID: taskID, answer: answer)
        if let index = tasks.firstIndex(where: { $0.id == taskID }) { tasks[index] = task }
        return task
    }

    func cancel(taskID: String) async throws -> RemoteTask {
        let task = try await client.cancel(taskID: taskID)
        if let index = tasks.firstIndex(where: { $0.id == taskID }) { tasks[index] = task }
        return task
    }

    func reportContent(id: String) async throws -> ReportContentEnvelope {
        try await client.report(id: id)
    }

    func download(report: ReportRecord) async throws -> URL {
        try await client.download(report: report)
    }

    func taskArtifactData(taskID: String) async throws -> Data {
        try await client.taskArtifactData(taskID: taskID)
    }

    func downloadArtifact(task: RemoteTask) async throws -> URL {
        try await client.downloadArtifact(task: task)
    }

    func refreshChats(showLoading: Bool = false) async {
        if showLoading { isLoading = true }
        defer { if showLoading { isLoading = false } }
        do {
            chats = try await client.listChats().conversations
            if let activeChatID,
               !chats.contains(where: { $0.id == activeChatID }) {
                self.activeChatID = nil
                chatMessages = []
                if let activeChatKey { UserDefaults.standard.removeObject(forKey: activeChatKey) }
            }
            errorMessage = ""
        } catch {
            guard !error.isRiverBankCancellation else { return }
            errorMessage = error.localizedDescription
        }
    }

    @discardableResult
    func newChat() async throws -> ChatConversation {
        let conversation = try await client.createChat()
        chats.insert(conversation, at: 0)
        activeChatID = conversation.id
        chatMessages = []
        if let activeChatKey { UserDefaults.standard.set(conversation.id, forKey: activeChatKey) }
        return conversation
    }

    func selectChat(_ conversation: ChatConversation) async {
        activeChatID = conversation.id
        if let activeChatKey { UserDefaults.standard.set(conversation.id, forKey: activeChatKey) }
        await refreshChatMessages()
    }

    func refreshChatMessages() async {
        guard let activeChatID else {
            chatMessages = []
            return
        }
        do {
            let envelope = try await client.chatMessages(conversationID: activeChatID)
            chatMessages = envelope.messages
            if let index = chats.firstIndex(where: { $0.id == envelope.conversation.id }) {
                chats[index] = envelope.conversation
            }
            errorMessage = ""
        } catch {
            guard !error.isRiverBankCancellation else { return }
            errorMessage = error.localizedDescription
        }
    }

    func sendChat(
        _ content: String,
        attachments: [ChatUploadAttachment] = []
    ) async throws {
        let conversationID: String
        if let activeChatID {
            conversationID = activeChatID
        } else {
            conversationID = (try await newChat()).id
        }
        let turn = try await client.sendChatMessage(
            conversationID: conversationID,
            content: content,
            attachments: attachments
        )
        chatMessages.append(turn.userMessage)
        chatMessages.append(turn.assistantMessage)
        await refreshChats()
    }

    func stopChatResponse() async throws {
        guard let activeChatID,
              let message = chatMessages.last(where: { $0.isAssistant && $0.isActive }) else {
            return
        }
        _ = try await client.cancelChatMessage(
            conversationID: activeChatID,
            messageID: message.id
        )
        await refreshChatMessages()
    }

    func deleteChat(_ conversation: ChatConversation) async throws {
        try await client.deleteChat(id: conversation.id)
        chats.removeAll { $0.id == conversation.id }
        if activeChatID == conversation.id {
            activeChatID = nil
            chatMessages = []
            if let activeChatKey { UserDefaults.standard.removeObject(forKey: activeChatKey) }
        }
    }
}
