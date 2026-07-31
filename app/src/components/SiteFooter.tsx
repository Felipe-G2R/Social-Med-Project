export default function SiteFooter() {
  const year = new Date().getFullYear();

  return (
    <footer className="ssm-footer">
      <div className="ssm-footer__inner">
        <span className="ssm-footer__copy">
          Todos os direitos reservados &copy; Social Selling Med {year}
        </span>
        <span className="ssm-footer__cnpj">CNPJ 63.059.905/0001-57</span>
      </div>
    </footer>
  );
}
