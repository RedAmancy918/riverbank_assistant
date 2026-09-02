import Foundation

@MainActor
final class AppStore: ObservableObject {
    @Published var tasks: [RemoteTask] = []
    @Published var reports: [ReportRecord] = []
    @Published var chats: [ChatConversation] = []
    @Published var chatMessages: [ChatMessage] = []
    @Published var activeChatID: String?
    @Published var server: String
    @Published var token: String
    @Published var errorMessage = ""
    @Published var connectionMessage = "尚未检测"
    @Published var isLoading = false

    private let serverKey = "riverbank.server"
    private let tokenAccount = "pairing-token"
    private let activeChatKey = "riverbank.active-chat"

    init() {
        server = UserDefaults.standard.string(forKey: serverKey)
            ?? "https://riverbank-tech.tail0acdab.ts.net/assistant"
        token = KeychainStore.read(tokenAccount)
        activeChatID = UserDefaults.standard.string(forKey: activeChatKey)
    }

    private var client: APIClient {
        APIClient(server: server, token: token)
    }

    func saveSettings(server: String, token: String) {
        self.server = server.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        self.token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        UserDefaults.standard.set(self.server, forKey: serverKey)
        KeychainStore.write(self.token, account: tokenAccount)
        connectionMessage = "设置已保存"
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

    func createTask(title: String, prompt: String, kind: String) async throws -> RemoteTask {
        let task = try await client.createTask(title: title, prompt: prompt, kind: kind)
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

    func refreshChats(showLoading: Bool = false) async {
        if showLoading { isLoading = true }
        defer { if showLoading { isLoading = false } }
        do {
            chats = try await client.listChats().conversations
            if let activeChatID,
               !chats.contains(where: { $0.id == activeChatID }) {
                self.activeChatID = nil
                chatMessages = []
                UserDefaults.standard.removeObject(forKey: activeChatKey)
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
        UserDefaults.standard.set(conversation.id, forKey: activeChatKey)
        return conversation
    }

    func selectChat(_ conversation: ChatConversation) async {
        activeChatID = conversation.id
        UserDefaults.standard.set(conversation.id, forKey: activeChatKey)
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

    func sendChat(_ content: String) async throws {
        let conversationID: String
        if let activeChatID {
            conversationID = activeChatID
        } else {
            conversationID = (try await newChat()).id
        }
        let turn = try await client.sendChatMessage(
            conversationID: conversationID,
            content: content
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
            UserDefaults.standard.removeObject(forKey: activeChatKey)
        }
    }
}
