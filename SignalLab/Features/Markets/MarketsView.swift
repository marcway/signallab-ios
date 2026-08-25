import SwiftUI

struct MarketsView: View {
    let instruments: [Instrument]

    var body: some View {
        NavigationStack {
            List(instruments) { instrument in
                VStack(alignment: .leading) {
                    Text(instrument.symbol)
                        .font(.headline)
                    Text(instrument.displayName)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Markets")
        }
    }
}

#Preview {
    MarketsView(instruments: LocalInstrumentCatalog.instruments)
}
