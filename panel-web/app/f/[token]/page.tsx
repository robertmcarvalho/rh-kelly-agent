type Props = { params: { token: string } };

export default async function FormPage({ params }: Props) {
  const { token } = params;
  return (
    <main style={{padding: 24, fontFamily: 'system-ui, sans-serif'}}>
      <h1>Formulário de Cadastro</h1>
      <p>Token: {token}</p>
      <p>Esta é uma página placeholder. A próxima etapa é implementar as seções do formulário e uploads para GCS via URLs assinadas.</p>
    </main>
  );
}

